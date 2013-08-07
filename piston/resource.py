import sys, inspect, re

from django.http import (HttpResponse, Http404, HttpResponseNotAllowed,
    HttpResponseForbidden, HttpResponseServerError)
from django.views.debug import ExceptionReporter
from django.views.decorators.vary import vary_on_headers
from django.conf import settings
from django.core.mail import send_mail, EmailMessage
from django.db.models.query import QuerySet
from django.http import Http404

from emitters import Emitter
from handler import typemapper
from doc import HandlerMethod
from authentication import NoAuthentication
from utils import coerce_put_post, FormValidationError, HttpStatusCode, BadRangeException
from utils import rc, format_error, translate_mime, MimerDataException

CHALLENGE = object()

class Resource(object):
    """
    Resource. Create one for your URL mappings, just
    like you would with Django. Takes one argument,
    the handler. The second argument is optional, and
    is an authentication handler. If not specified,
    `NoAuthentication` will be used by default.
    """
    callmap = { 'GET': 'read', 'POST': 'create',
                'PUT': 'update', 'DELETE': 'delete' }

    range_re = re.compile("^items=(\d*)-(\d*)$")

    def __init__(self, handler, authentication=None):
        if not callable(handler):
            raise AttributeError, "Handler not callable."

        self.handler = handler()
        self.csrf_exempt = getattr(self.handler, 'csrf_exempt', True)

        if not authentication:
            self.authentication = (NoAuthentication(),)
        elif isinstance(authentication, (list, tuple)):
            self.authentication = authentication
        else:
            self.authentication = (authentication,)

        # Erroring
        self.email_errors = getattr(settings, 'PISTON_EMAIL_ERRORS', True)
        self.display_errors = getattr(settings, 'PISTON_DISPLAY_ERRORS', True)
        self.stream = getattr(settings, 'PISTON_STREAM_OUTPUT', False)
        
        # Paging
        paging_params = getattr(settings, 'PISTON_PAGINATION_PARAMS', ('offset', 'limit'))
        self.paging_offset = paging_params[0]
        self.paging_limit = paging_params[1]

    def determine_emitter(self, request, *args, **kwargs):
        """
        Function for determening which emitter to use
        for output. It lives here so you can easily subclass
        `Resource` in order to change how emission is detected.

        You could also check for the `Accept` HTTP header here,
        since that pretty much makes sense. Refer to `Mimer` for
        that as well.
        """
        em = kwargs.pop('emitter_format', None)

        if not em:
            em = request.GET.get('format', 'json')

        return em

    def form_validation_response(self, e):
        """
        Method to return form validation error information. 
        You will probably want to override this in your own
        `Resource` subclass.
        """
        resp = rc.BAD_REQUEST
        resp.write(' ' + unicode(e.form.errors))
        return resp

    @property
    def anonymous(self):
        """
        Gets the anonymous handler. Also tries to grab a class
        if the `anonymous` value is a string, so that we can define
        anonymous handlers that aren't defined yet (like, when
        you're subclassing your basehandler into an anonymous one.)
        """
        if hasattr(self.handler, 'anonymous'):
            anon = self.handler.anonymous

            if callable(anon):
                return anon

            for klass in typemapper.keys():
                if anon == klass.__name__:
                    return klass

        return None

    def authenticate(self, request, rm):
        actor, anonymous = False, True

        for authenticator in self.authentication:
            if not authenticator.is_authenticated(request):
                if self.anonymous and \
                    rm in self.anonymous.allowed_methods:

                    actor, anonymous = self.anonymous(), True
                else:
                    actor, anonymous = authenticator.challenge, CHALLENGE
            else:
                return self.handler, self.handler.is_anonymous

        return actor, anonymous

    @vary_on_headers('Authorization')
    def __call__(self, request, *args, **kwargs):
        """
        NB: Sends a `Vary` header so we don't cache requests
        that are different (OAuth stuff in `Authorization` header.)
        """
        rm = request.method.upper()

        # Django's internal mechanism doesn't pick up
        # PUT request, so we trick it a little here.
        if rm == "PUT":
            coerce_put_post(request)

        actor, anonymous = self.authenticate(request, rm)

        if anonymous is CHALLENGE:
            return actor()
        else:
            handler = actor

        # Translate nested datastructs into `request.data` here.
        if rm in ('POST', 'PUT'):
            try:
                translate_mime(request)
            except MimerDataException:
                return rc.BAD_REQUEST
            if not hasattr(request, 'data'):
                if rm == 'POST':
                    request.data = request.POST
                else:
                    request.data = request.PUT

        if not rm in handler.allowed_methods:
            return HttpResponseNotAllowed(handler.allowed_methods)

        meth = getattr(handler, self.callmap.get(rm, ''), None)
        if not meth:
            raise Http404

        # Support emitter both through (?P<emitter_format>) and ?format=emitter.
        em_format = self.determine_emitter(request, *args, **kwargs)

        kwargs.pop('emitter_format', None)

        # Clean up the request object a bit, since we might
        # very well have `oauth_`-headers in there, and we
        # don't want to pass these along to the handler.
        request = self.cleanup_request(request)
        
        hm = HandlerMethod(meth)
        sig = hm.signature

        try:
            result = meth(request, *args, **kwargs)
        except Exception, e:
            result = self.error_handler(e, request, meth, em_format)

        try:
            emitter, ct = Emitter.get(em_format)
            fields = handler.fields

            if hasattr(handler, 'list_fields') and isinstance(result, (list, tuple, QuerySet)):
                fields = handler.list_fields
        except ValueError:
            result = rc.BAD_REQUEST
            result.content = "Invalid output format specified '%s'." % em_format
            return result

        status_code = 200

        # If we're looking at a response object which contains non-string
        # content, then assume we should use the emitter to format that 
        # content
        if isinstance(result, HttpResponse) and not isinstance(result, str):
            status_code = result.status_code
            # Note: We can't use result.content here because that method attempts
            # to convert the content into a string which we don't want. 
            # when _is_string is False _container is the raw data
            result = result._container
            if sig:
                msg += 'Signature should be: %s' % sig
     
        srl = emitter(result, typemapper, handler, fields, anonymous)

        try:
            """
            Decide whether or not we want a generator here,
            or we just want to buffer up the entire result
            before sending it to the client. Won't matter for
            smaller datasets, but larger will have an impact.
            """
            if self.stream: stream = srl.stream_render(request)
            else: stream = srl.render(request)

            if not isinstance(stream, HttpResponse):
                resp = HttpResponse(stream, mimetype=ct, status=status_code)
            else:
                resp = stream

            if self.display_errors:
                msg += '\n\nException was: %s' % str(e)
            resp.streaming = self.stream
            result.content = format_error(msg)
        except Http404:
            return rc.NOT_FOUND
        except HttpStatusCode, e:
            return e.response
        except Exception, e:
            """
            On errors (like code errors), we'd like to be able to
            give crash reports to both admins and also the calling
            user. There's two setting parameters for this:

            Parameters::
             - `PISTON_EMAIL_ERRORS`: Will send a Django formatted
               error email to people in `settings.ADMINS`.
             - `PISTON_DISPLAY_ERRORS`: Will return a simple traceback
               to the caller, so he can tell you what error they got.

            If `PISTON_DISPLAY_ERRORS` is not enabled, the caller will
            receive a basic "500 Internal Server Error" message.
            """
            exc_type, exc_value, tb = sys.exc_info()
            rep = ExceptionReporter(request, exc_type, exc_value, tb.tb_next)
            if self.email_errors:
                self.email_exception(rep)
            if self.display_errors:
                return HttpResponseServerError(
                    format_error('\n'.join(rep.format_exception())))
            else:
                raise

        content_range = None
        if isinstance(result, QuerySet):
            """
            Limit results based on requested items. This is a based on
            HTTP 1.1 Partial GET, RFC 2616 sec 14.35, but is intended to
            operate on the record level rather than the byte level.  We
            will still respond with code 206 and a range header.
            """

            request_range = None
            
            if 'HTTP_RANGE' in request.META:
                """
                Parse the reange request header. HTTP proper expects Range, 
                but since we are deviating from the bytes nature, we will use 
                a non-standard syntax. This is expected to be of the format:

                    "items" "=" start "-" end

                E.g.,

                    Range: items=7-45

                """
                h = request.META['HTTP_RANGE'].strip()
                if h.startswith('items='):
                    m = self.range_re.match(h)
                    if m:
                        s, e = None, None
                        if m.group(1) != '': s = int(m.group(1))
                        if m.group(2) != '': e = int(m.group(2))
                        request_range = (s, e)
                    else:
                        resp = rc.BAD_REQUEST
                        resp.write(' malformed range header')
                        return resp 

            elif self.paging_offset in request.GET and self.paging_limit in request.GET:
                """
                Alternatively, parse the query string for paging parameters. Here,
                we ask for an offset and a limit, so we need to convert this to a 
                fixed start and end to accomodate the slicing. Both parameters must 
                be specified, but either may be left empty, exclusively, to produce
                the following behaviors:

                    * ?offset=n&limit= -> tail of list, beginning at item n
                    * ?offset=&limit=n -> trailing n items of list

                """
                try: offset = int(request.GET[self.paging_offset])
                except (ValueError, TypeError): offset = None

                try: limit = int(request.GET[self.paging_limit])
                except (ValueError, TypeError): limit = None

                if offset != None and limit != None: request_range = (offset, offset + limit - 1)
                elif offset != None: request_range = (offset, None)
                elif limit != None: request_range = (None, limit)

            if request_range:
                def get_range(start, end, total):
                    """
                    Normalizes the range for queryset slicing and response
                    header generation.  Checks that constraints defined
                    by the RFC hold, or raises exceptions.
                    """
                    
                    if start != None and start < 0: raise BadRangeException('negative ranges not allowed')
                    if end != None and end < 0: raise BadRangeException('negative ranges not allowed')
                    
                    range_start = start
                    range_end = end
                    last = total - 1

                    if range_start != None and range_end != None:
                        """
                        Basic, well-formed range.  Make sure that requested range is
                        between 0 and last inclusive, and that start <= end. 
                        """
                        if range_start > last: raise BadRangeException("start gt last")
                        if range_start > range_end: raise BadRangeException("start lt end")
                        if range_end > last: range_end = last;

                    elif range_start != None:
                        """
                        Requsting range_start through last.  Make sure range_start is
                        valid and set range_end to last.
                        """
                        if range_start > last: raise BadRangeException("start gt last")
                        range_end = last

                    elif range_end != None:
                        """
                        Requesting range_end items from the tail of the result set.
                        If range_end > last, the entire resultset is returned.  otherwise
                        range_start must be last - range_end.
                        """
                        if range_end > last:
                            range_start = 0
                            range_end = last
                        else:
                            range_start = last - range_end + 1
                            range_end = last

                    else:
                        raise BadRangeException("no start or end")
                  
                    assert range_start != None
                    assert range_end != None 
                    return (range_start, range_end)


                try:
                    total = result.count()
                    start, end = get_range(request_range[0], request_range[1], total)
                    result = result[start:end + 1]
                    content_range = "items %i-%i/%i" % (start, end, total)
                except BadRangeException, e:
                    resp = rc.BAD_RANGE
                    resp.write("\n%s" % e.value)
                    return resp


        emitter, ct = Emitter.get(em_format)
        fields = handler.fields
        if hasattr(handler, 'list_fields') and (
                isinstance(result, list) or isinstance(result, QuerySet)):
            fields = handler.list_fields

        srl = emitter(result, typemapper, handler, fields, anonymous)

        try:
            """
            Decide whether or not we want a generator here,
            or we just want to buffer up the entire result
            before sending it to the client. Won't matter for
            smaller datasets, but larger will have an impact.
            """
            if self.stream: stream = srl.stream_render(request)
            else: stream = srl.render(request)

            if not isinstance(stream, HttpResponse):
                resp = HttpResponse(stream, mimetype=ct, status=status_code)
            else:
                resp = stream

            resp.streaming = self.stream

            if content_range:
                resp.status_code = 206
                resp['Content-Range'] = content_range

            return resp
        except HttpStatusCode, e:
            return e.response

    @staticmethod
    def cleanup_request(request):
        """
        Removes `oauth_` keys from various dicts on the
        request object, and returns the sanitized version.
        """
        for method_type in ('GET', 'PUT', 'POST', 'DELETE'):
            block = getattr(request, method_type, { })

            if True in [ k.startswith("oauth_") for k in block.keys() ]:
                sanitized = block.copy()

                for k in sanitized.keys():
                    if k.startswith("oauth_"):
                        sanitized.pop(k)

                setattr(request, method_type, sanitized)

        return request

    # --

    def email_exception(self, reporter):
        subject = "Piston crash report"
        html = reporter.get_traceback_html()

        message = EmailMessage(settings.EMAIL_SUBJECT_PREFIX+subject,
                                html, settings.SERVER_EMAIL,
                                [ admin[1] for admin in settings.ADMINS ])

        message.content_subtype = 'html'
        message.send(fail_silently=True)


    def error_handler(self, e, request, meth, em_format):
        """
        Override this method to add handling of errors customized for your 
        needs
        """
        if isinstance(e, FormValidationError):
            return self.form_validation_response(e)

        elif isinstance(e, TypeError):
            result = rc.BAD_REQUEST
            hm = HandlerMethod(meth)
            sig = hm.signature

            msg = 'Method signature does not match.\n\n'

            if sig:
                msg += 'Signature should be: %s' % sig
            else:
                msg += 'Resource does not expect any parameters.'

            if self.display_errors:
                msg += '\n\nException was: %s' % str(e)

            result.content = format_error(msg)
            return result
        elif isinstance(e, Http404):
            return rc.NOT_FOUND

        elif isinstance(e, HttpStatusCode):
            return e.response
 
        else: 
            """
            On errors (like code errors), we'd like to be able to
            give crash reports to both admins and also the calling
            user. There's two setting parameters for this:

            Parameters::
             - `PISTON_EMAIL_ERRORS`: Will send a Django formatted
               error email to people in `settings.ADMINS`.
             - `PISTON_DISPLAY_ERRORS`: Will return a simple traceback
               to the caller, so he can tell you what error they got.

            If `PISTON_DISPLAY_ERRORS` is not enabled, the caller will
            receive a basic "500 Internal Server Error" message.
            """
            exc_type, exc_value, tb = sys.exc_info()
            rep = ExceptionReporter(request, exc_type, exc_value, tb.tb_next)
            if self.email_errors:
                self.email_exception(rep)
            if self.display_errors:
                return HttpResponseServerError(
                    format_error('\n'.join(rep.format_exception())))
            else:
                raise
