# -*- coding: utf-8 -*-

import base64
try:
    import cPickle as pickle
except ImportError:
    import pickle
import getpass
import logging
import sys
import threading

from django.contrib.auth.models import User
from django.core import mail
from django.core.handlers.wsgi import WSGIRequest, WSGIHandler
from django.core.urlresolvers import reverse
from django.core.signals import got_request_exception
from django.core.servers import basehttp
from django.test.client import Client
from django.test import TestCase
from django.utils.encoding import smart_unicode

from sentry import settings
from sentry.helpers import transform
from sentry.models import Message, GroupedMessage
from sentry.client.base import SentryClient
from sentry.client.models import sentry_exception_handler, get_client

from models import TestModel, DuplicateKeyModel

class TestServerThread(threading.Thread):
    """Thread for running a http server while tests are running."""

    def __init__(self, address, port):
        self.address = address
        self.port = port
        self._stopevent = threading.Event()
        self.started = threading.Event()
        self.error = None
        super(TestServerThread, self).__init__()

    def run(self):
        """Sets up test server and database and loops over handling http requests."""
        from django.conf import settings
        try:
            handler = basehttp.AdminMediaHandler(WSGIHandler())
            server_address = (self.address, self.port)
            httpd = basehttp.StoppableWSGIServer(server_address, basehttp.WSGIRequestHandler)
            httpd.set_app(handler)
            self.started.set()
        except basehttp.WSGIServerException, e:
            self.error = e
            self.started.set()
            return

        # Must do database stuff in this new thread if database in memory.
        if settings.DATABASE_ENGINE == 'sqlite3' \
            and (not settings.TEST_DATABASE_NAME or settings.TEST_DATABASE_NAME == ':memory:'):
            # Import the fixture data into the test database.
            if hasattr(self, 'fixtures'):
                # We have to use this slightly awkward syntax due to the fact
                # that we're using *args and **kwargs together.
                call_command('loaddata', *self.fixtures, **{'verbosity': 0})

        # Loop until we get a stop event.
        while not self._stopevent.isSet():
            httpd.handle_request()

    def join(self, timeout=None):
        """Stop the thread and wait for it to finish."""
        self._stopevent.set()
        threading.Thread.join(self, timeout)

def conditional_on_module(module):
    def wrapped(func):
        def inner(self, *args, **kwargs):
            try:
                __import__(module)
            except ImportError:
                print "Skipping test: %s.%s" % (self.__class__.__name__, func.__name__)
            else:
                return func(self, *args, **kwargs)
        return inner
    return wrapped

class RequestFactory(Client):
    # Used to generate request objects.
    def request(self, **request):
        environ = {
            'HTTP_COOKIE': self.cookies,
            'PATH_INFO': '/',
            'QUERY_STRING': '',
            'REQUEST_METHOD': 'GET',
            'SCRIPT_NAME': '',
            'SERVER_NAME': 'testserver',
            'SERVER_PORT': 80,
            'SERVER_PROTOCOL': 'HTTP/1.1',
        }
        environ.update(self.defaults)
        environ.update(request)
        return WSGIRequest(environ)
 
RF = RequestFactory()

class SentryTestCase(TestCase):
    urls = 'sentry.tests.urls'

    def setUp(self):
        self._handlers = None
        self._level = None
        self.logger = logging.getLogger('sentry')
        self.logger.addHandler(logging.StreamHandler())
        Message.objects.all().delete()
        GroupedMessage.objects.all().delete()

    def tearDown(self):
        self.tearDownHandler()
        
    def setUpHandler(self):
        self.tearDownHandler()
        from sentry.client.handlers import SentryHandler
        
        logger = logging.getLogger()
        self._handlers = logger.handlers
        self._level = logger.level

        for h in self._handlers:
            # TODO: fix this, for now, I don't care.
            logger.removeHandler(h)
    
        logger.setLevel(logging.DEBUG)
        sentry_handler = SentryHandler()
        logger.addHandler(sentry_handler)
    
    def tearDownHandler(self):
        if self._handlers is None:
            return
        
        logger = logging.getLogger()
        logger.removeHandler(logger.handlers[0])
        for h in self._handlers:
            logger.addHandler(h)
        
        logger.setLevel(self._level)
        self._handlers = None
        
    def testLogger(self):
        logger = logging.getLogger()
        
        self.setUpHandler()

        logger.error('This is a test error')
        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)
        last = Message.objects.get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, 'This is a test error')

        logger.warning('This is a test warning')
        self.assertEquals(Message.objects.count(), 2)
        self.assertEquals(GroupedMessage.objects.count(), 2)
        last = Message.objects.all().order_by('-id')[0:1].get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.level, logging.WARNING)
        self.assertEquals(last.message, 'This is a test warning')
        
        logger.error('This is a test error')
        self.assertEquals(Message.objects.count(), 3)
        self.assertEquals(GroupedMessage.objects.count(), 2)
        last = Message.objects.all().order_by('-id')[0:1].get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, 'This is a test error')
    
        logger = logging.getLogger('test')
        logger.info('This is a test info')
        self.assertEquals(Message.objects.count(), 4)
        self.assertEquals(GroupedMessage.objects.count(), 3)
        last = Message.objects.all().order_by('-id')[0:1].get()
        self.assertEquals(last.logger, 'test')
        self.assertEquals(last.level, logging.INFO)
        self.assertEquals(last.message, 'This is a test info')
        
        logger.info('This is a test info with a url', extra=dict(url='http://example.com'))
        self.assertEquals(Message.objects.count(), 5)
        self.assertEquals(GroupedMessage.objects.count(), 4)
        last = Message.objects.all().order_by('-id')[0:1].get()
        self.assertEquals(last.url, 'http://example.com')
        
        try:
            raise ValueError('This is a test ValueError')
        except ValueError:
            logger.info('This is a test info with an exception', exc_info=sys.exc_info())
            self.assertEquals(Message.objects.count(), 6)
            self.assertEquals(GroupedMessage.objects.count(), 5)
            last = Message.objects.all().order_by('-id')[0:1].get()
            self.assertEquals(last.class_name, 'ValueError')
            self.assertEquals(last.message, 'This is a test info with an exception')
            self.assertTrue(last.data.get('__sentry__', {}).get('exc'))
    
        self.tearDownHandler()
    
    def testMiddleware(self):
        Message.objects.all().delete()
        GroupedMessage.objects.all().delete()
        
        request = RF.get("/", REMOTE_ADDR="127.0.0.1:8000")

        try:
            Message.objects.get(id=999999999)
        except Message.DoesNotExist, exc:
            sentry_exception_handler(request=request, sender=self)
        else:
            self.fail('Unable to create `Message` entry.')
        
        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)
        last = Message.objects.get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.class_name, 'DoesNotExist')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, smart_unicode(exc))
        
    def testAPI(self):
        try:
            Message.objects.get(id=999999989)
        except Message.DoesNotExist, exc:
            get_client().create_from_exception(exc)
        else:
            self.fail('Unable to create `Message` entry.')

        try:
            Message.objects.get(id=999999989)
        except Message.DoesNotExist, exc:
            error = get_client().create_from_exception()
            self.assertTrue(error.data.get('__sentry__', {}).get('exc'))
        else:
            self.fail('Unable to create `Message` entry.')

        
        self.assertEquals(Message.objects.count(), 2)
        self.assertEquals(GroupedMessage.objects.count(), 2)
        last = Message.objects.all().order_by('-id')[0:1].get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.class_name, 'DoesNotExist')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, smart_unicode(exc))
        
        get_client().create_from_text('This is an error', level=logging.DEBUG)
        
        self.assertEquals(Message.objects.count(), 3)
        self.assertEquals(GroupedMessage.objects.count(), 3)
        last = Message.objects.all().order_by('-id')[0:1].get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.level, logging.DEBUG)
        self.assertEquals(last.message, 'This is an error')
        
    def testAlternateDatabase(self):
        settings.DATABASE_USING = 'default'
        
        try:
            Message.objects.get(id=999999979)
        except Message.DoesNotExist, exc:
            get_client().create_from_exception(exc)
        else:
            self.fail('Unable to create `Message` entry.')
            
        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)
        last = Message.objects.get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.class_name, 'DoesNotExist')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, smart_unicode(exc))

        settings.DATABASE_USING = None
    
    def testIncorrectUnicode(self):
        self.setUpHandler()
        
        cnt = Message.objects.count()
        value = 'רונית מגן'

        error = get_client().create_from_text(value)
        self.assertEquals(Message.objects.count(), cnt+1)
        self.assertEquals(error.message, value)

        logging.info(value)
        self.assertEquals(Message.objects.count(), cnt+2)

        x = TestModel.objects.create(data={'value': value})
        logging.warn(x)
        self.assertEquals(Message.objects.count(), cnt+3)

        try:
            raise SyntaxMessage(value)
        except Exception, exc:
            logging.exception(exc)
            logging.info('test', exc_info=sys.exc_info())
        self.assertEquals(Message.objects.count(), cnt+5)
        
        self.tearDownHandler()

    def testCorrectUnicode(self):
        self.setUpHandler()
        
        cnt = Message.objects.count()
        value = 'רונית מגן'.decode('utf-8')

        error = get_client().create_from_text(value)
        self.assertEquals(Message.objects.count(), cnt+1)
        self.assertEquals(error.message, value)

        logging.info(value)
        self.assertEquals(Message.objects.count(), cnt+2)

        x = TestModel.objects.create(data={'value': value})
        logging.warn(x)
        self.assertEquals(Message.objects.count(), cnt+3)

        try:
            raise SyntaxMessage(value)
        except Exception, exc:
            logging.exception(exc)
            logging.info('test', exc_info=sys.exc_info())
        self.assertEquals(Message.objects.count(), cnt+5)
        
        self.tearDownHandler()
    
    def testLongURLs(self):
        # Fix: #6 solves URLs > 200 characters
        error = get_client().create_from_text('hello world', url='a'*210)
        self.assertEquals(error.url, 'a'*200)
        self.assertEquals(error.data['url'], 'a'*210)
    
    def testThrashing(self):
        settings.THRASHING_LIMIT = 10
        settings.THRASHING_TIMEOUT = 60
        
        Message.objects.all().delete()
        GroupedMessage.objects.all().delete()
        
        for i in range(0, 50):
            get_client().create_from_text('hi')
        
        self.assertEquals(Message.objects.count(), settings.THRASHING_LIMIT)
    
    def testSignals(self):
        request = RF.get("/", REMOTE_ADDR="127.0.0.1:8000")

        try:
            Message.objects.get(id=999999999)
        except Message.DoesNotExist, exc:
            got_request_exception.send(sender=self.__class__, request=request)
        else:
            self.fail('Expected an exception.')
            
        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)
        last = Message.objects.get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.class_name, 'DoesNotExist')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, smart_unicode(exc))

    def testSignalsWithoutRequest(self):
        request = RF.get("/", REMOTE_ADDR="127.0.0.1:8000")

        try:
            Message.objects.get(id=999999999)
        except Message.DoesNotExist, exc:
            got_request_exception.send(sender=self.__class__, request=None)
        else:
            self.fail('Expected an exception.')
            
        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)
        last = Message.objects.get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.class_name, 'DoesNotExist')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, smart_unicode(exc))

    def testNoThrashing(self):
        prev = settings.THRASHING_LIMIT
        settings.THRASHING_LIMIT = 0
        
        Message.objects.all().delete()
        GroupedMessage.objects.all().delete()
        
        for i in range(0, 50):
            get_client().create_from_text('hi')
        
        self.assertEquals(Message.objects.count(), 50)

        settings.THRASHING_LIMIT = prev

    def testDatabaseMessage(self):
        from django.db import connection
        
        try:
            cursor = connection.cursor()
            cursor.execute("select foo")
        except:
            got_request_exception.send(sender=self.__class__)

        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)

    def testIntegrityMessage(self):
        DuplicateKeyModel.objects.create()
        try:
            DuplicateKeyModel.objects.create()
        except:
            got_request_exception.send(sender=self.__class__)
        else:
            self.fail('Excepted an IntegrityMessage to be raised.')

        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)

    def testViewException(self):
        self.assertRaises(Exception, self.client.get, reverse('sentry-raise-exc'))
        
        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)
        last = Message.objects.get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.class_name, 'Exception')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, 'view exception')
        self.assertEquals(last.view, 'sentry.tests.views.raise_exc')

    def testRequestMiddlwareException(self):
        from django.conf import settings
        orig = list(settings.MIDDLEWARE_CLASSES)
        settings.MIDDLEWARE_CLASSES = orig + ['sentry.tests.middleware.BrokenRequestMiddleware',]
        
        self.assertRaises(ImportError, self.client.get, reverse('sentry'))
        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)
        last = Message.objects.get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.class_name, 'ImportError')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, 'request')
        self.assertEquals(last.view, 'sentry.tests.middleware.process_request')
        
        settings.MIDDLEWARE_CLASSES = orig

    # XXX: Django doesn't handle response middleware exceptions (yet)
    # def testResponseMiddlwareException(self):
    #     from django.conf import settings
    #     orig = list(settings.MIDDLEWARE_CLASSES)
    #     settings.MIDDLEWARE_CLASSES = orig + ['sentry.tests.middleware.BrokenResponseMiddleware',]
    #     
    #     self.assertRaises(ImportError, self.client.get, reverse('sentry'))
    #     self.assertEquals(Message.objects.count(), 1)
    #     self.assertEquals(GroupedMessage.objects.count(), 1)
    #     last = Message.objects.get()
    #     self.assertEquals(last.logger, 'root')
    #     self.assertEquals(last.class_name, 'ImportError')
    #     self.assertEquals(last.level, logging.ERROR)
    #     self.assertEquals(last.message, 'response')
    #     self.assertEquals(last.view, 'sentry.tests.middleware.process_response')
    #     
    #     settings.MIDDLEWARE_CLASSES = orig

    def testViewMiddlewareException(self):
        from django.conf import settings
        orig = list(settings.MIDDLEWARE_CLASSES)
        settings.MIDDLEWARE_CLASSES = orig + ['sentry.tests.middleware.BrokenViewMiddleware',]
        
        self.assertRaises(ImportError, self.client.get, reverse('sentry'))
        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)
        last = Message.objects.get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.class_name, 'ImportError')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, 'view')
        self.assertEquals(last.view, 'sentry.tests.middleware.process_view')
        
        settings.MIDDLEWARE_CLASSES = orig

    def testSettingName(self):
        orig = settings.NAME
        settings.NAME = 'foo'
        
        self.assertRaises(Exception, self.client.get, reverse('sentry-raise-exc'))

        self.assertEquals(Message.objects.count(), 1)
        self.assertEquals(GroupedMessage.objects.count(), 1)
        last = Message.objects.get()
        self.assertEquals(last.logger, 'root')
        self.assertEquals(last.class_name, 'Exception')
        self.assertEquals(last.level, logging.ERROR)
        self.assertEquals(last.message, 'view exception')
        self.assertEquals(last.server_name, 'foo')
        self.assertEquals(last.view, 'sentry.tests.views.raise_exc')
        
        settings.NAME = orig

class SentryViewsTest(TestCase):
    urls = 'sentry.tests.urls'
    fixtures = ['sentry/tests/fixtures/views.json']
    
    def setUp(self):
        settings.DATABASE_USING = None
        self._handlers = None
        self._level = None
        settings.DEBUG = False
        self.user = User(username="admin", email="admin@localhost", is_staff=True, is_superuser=True)
        self.user.set_password('admin')
        self.user.save()
    
    def tearDown(self):
        self.tearDownHandler()
        
    def setUpHandler(self):
        self.tearDownHandler()
        from sentry.client.handlers import SentryHandler
        
        logger = logging.getLogger()
        self._handlers = logger.handlers
        self._level = logger.level

        for h in self._handlers:
            # TODO: fix this, for now, I don't care.
            logger.removeHandler(h)
    
        logger.setLevel(logging.DEBUG)
        sentry_handler = SentryHandler()
        logger.addHandler(sentry_handler)
    
    def tearDownHandler(self):
        if self._handlers is None:
            return
        
        logger = logging.getLogger()
        logger.removeHandler(logger.handlers[0])
        for h in self._handlers:
            logger.addHandler(h)
        
        logger.setLevel(self._level)
        self._handlers = None

    def testTestAuth(self):
        resp = self.client.get(reverse('sentry'), follow=True)
        self.assertEquals(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'sentry/login.html')

    def testDashboard(self):
        self.client.login(username='admin', password='admin')
        resp = self.client.get(reverse('sentry'), follow=True)
        self.assertEquals(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'sentry/index.html')
        group = resp.context['message_list'][0]
        self.assertEquals(group.times_seen, 7)
        self.assertEquals(group.class_name, 'AttributeError')
        self.assertEquals(len(resp.context['message_list']), 4)

    def testGroup(self):
        self.client.login(username='admin', password='admin')
        resp = self.client.get(reverse('sentry-group', args=[2]), follow=True)
        self.assertEquals(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'sentry/group.html')

class RemoteSentryTest(TestCase):
    urls = 'sentry.tests.urls'
    
    def start_test_server(self, address='localhost', port=8000):
        """Creates a live test server object (instance of WSGIServer)."""
        self.server_thread = TestServerThread(address, port)
        self.server_thread.start()
        self.server_thread.started.wait()
        if self.server_thread.error:
            raise self.server_thread.error

    def stop_test_server(self):
        if self.server_thread:
            self.server_thread.join()
    
    def setUp(self):
        self.server_thread = None
        settings.REMOTE_URL = ['http://localhost:8000%s' % reverse('sentry-store')]
        logger = logging.getLogger('sentry')
        for h in logger.handlers:
            logger.removeHandler(h)
        logger.addHandler(logging.StreamHandler())

    def tearDown(self):
        self.stop_test_server()
        settings.REMOTE_URL = None

    def testNoKey(self):
        resp = self.client.post(reverse('sentry-store'))
        self.assertEquals(resp.status_code, 403)
        self.assertEquals(resp.content, 'Invalid credentials')

    def testNoData(self):
        resp = self.client.post(reverse('sentry-store'), {
            'key': settings.KEY,
        })
        self.assertEquals(resp.status_code, 403)
        self.assertEquals(resp.content, 'Missing data')

    def testBadData(self):
        resp = self.client.post(reverse('sentry-store'), {
            'key': settings.KEY,
            'data': 'hello world',
        })
        self.assertEquals(resp.status_code, 403)
        self.assertEquals(resp.content, 'Bad data')

    def testCorrectData(self):
        kwargs = {'message': 'hello', 'server_name': 'not_dcramer.local', 'level': 40}
        data = {
            
        }
        resp = self.client.post(reverse('sentry-store'), {
            'data': base64.b64encode(pickle.dumps(transform(kwargs)).encode('zlib')),
            'key': settings.KEY,
        })
        self.assertEquals(resp.status_code, 200)
        instance = Message.objects.get()
        self.assertEquals(instance.message, 'hello')
        self.assertEquals(instance.server_name, 'not_dcramer.local')
        self.assertEquals(instance.level, 40)

    # def testProcess(self):
    #     self.start_test_server()
    #     SentryClient.process(message='hello')
    #     instance = Message.objects.all().order_by('-id')[0]
    #     self.assertEquals(instance.message, 'hello')
    #     self.stop_test_server()
    # 
    # def testExternal(self):
    #     self.start_test_server()
    #     self.assertRaises(Exception, self.client.get, '/?test')
    #     instance = Message.objects.all().order_by('-id')[0]
    #     self.assertEquals(instance.message, 'view exception')
    #     self.assertEquals(instance.url, 'http://testserver/?test')
    #     self.stop_test_server()

class SentryFeedsTest(TestCase):
    fixtures = ['sentry/tests/fixtures/feeds.json']
    urls = 'sentry.tests.urls'
    
    def testMessageFeed(self):
        response = self.client.get(reverse('sentry-feed-messages'))
        self.assertEquals(response.status_code, 200)
        self.assertTrue(response.content.startswith('<?xml version="1.0" encoding="utf-8"?>'))
        self.assertTrue('<link>http://testserver/</link>' in response.content)
        self.assertTrue('<title>log messages</title>' in response.content)
        self.assertTrue('<link>http://testserver/group/1</link>' in response.content, response.content)
        self.assertTrue('<title>TypeError: exceptions must be old-style classes or derived from BaseException, not NoneType</title>' in response.content)

    def testSummaryFeed(self):
        response = self.client.get(reverse('sentry-feed-summaries'))
        self.assertEquals(response.status_code, 200)
        self.assertTrue(response.content.startswith('<?xml version="1.0" encoding="utf-8"?>'))
        self.assertTrue('<link>http://testserver/</link>' in response.content)
        self.assertTrue('<title>log summaries</title>' in response.content)
        self.assertTrue('<link>http://testserver/group/1</link>' in response.content, response.content)
        self.assertTrue('<title>(1) TypeError: exceptions must be old-style classes or derived from BaseException, not NoneType</title>' in response.content)

class SentryMailTest(TestCase):
    fixtures = ['sentry/tests/fixtures/mail.json']
    urls = 'sentry.tests.urls'
    
    def setUp(self):
        settings.ADMINS = ('%s@localhost' % getpass.getuser(),)
    
    def test_mail_admins(self):
        group = GroupedMessage.objects.get()
        self.assertEquals(len(mail.outbox), 0)
        group.mail_admins(fail_silently=False)
        self.assertEquals(len(mail.outbox), 1)

        out = mail.outbox[0]

        self.assertTrue('Traceback (most recent call last):' in out.body)
        self.assertTrue("COOKIES:{'commenter_name': 'admin'," in out.body)
        self.assertEquals(out.subject, '[Django] Error (EXTERNAL IP): /group/1')

    def test_mail_on_creation(self):
        settings.MAIL = True
        
        self.assertEquals(len(mail.outbox), 0)
        self.assertRaises(Exception, self.client.get, reverse('sentry-raise-exc'))
        self.assertEquals(len(mail.outbox), 1)
        self.assertRaises(Exception, self.client.get, reverse('sentry-raise-exc'))
        self.assertEquals(len(mail.outbox), 1)

        out = mail.outbox[0]

        self.assertTrue('Traceback (most recent call last):' in out.body)
        self.assertTrue("<Request" in out.body)
        self.assertEquals(out.subject, '[Django] Error (EXTERNAL IP): /trigger-500')

    def test_mail_on_duplication(self):
        settings.MAIL = True
        
        self.assertEquals(len(mail.outbox), 0)
        self.assertRaises(Exception, self.client.get, reverse('sentry-raise-exc'))
        self.assertEquals(len(mail.outbox), 1)
        self.assertRaises(Exception, self.client.get, reverse('sentry-raise-exc'))
        self.assertEquals(len(mail.outbox), 1)
        # XXX: why wont this work
        # group = GroupedMessage.objects.update(status=1)
        group = GroupedMessage.objects.all().order_by('-id')[0]
        group.status = 1
        group.save()
        self.assertRaises(Exception, self.client.get, reverse('sentry-raise-exc'))
        self.assertEquals(len(mail.outbox), 2)
        self.assertRaises(Exception, self.client.get, reverse('sentry-raise-exc'))
        self.assertEquals(len(mail.outbox), 2)

        out = mail.outbox[1]

        self.assertTrue('Traceback (most recent call last):' in out.body)
        self.assertTrue("<Request" in out.body)
        self.assertEquals(out.subject, '[Django] Error (EXTERNAL IP): /trigger-500')

class SentryHelpersTest(TestCase):
    def test_get_db_engine(self):
        from django.conf import settings
        from sentry.helpers import get_db_engine
        _databases = getattr(settings, 'DATABASES', {}).copy()
        _engine = settings.DATABASE_ENGINE
        
        settings.DATABASE_ENGINE = ''
        settings.DATABASES['default'] = {'ENGINE': 'blah.sqlite3'}
        
        self.assertEquals(get_db_engine(), 'sqlite3')

        settings.DATABASE_ENGINE = 'mysql'

        self.assertEquals(get_db_engine(), 'sqlite3')

        settings.DATABASES['default'] = {'ENGINE': 'blah.mysql'}

        self.assertEquals(get_db_engine(), 'mysql')
        
        settings.DATABASES = _databases
        settings.DATABASE_ENGINE = _engine

class SentryClientTest(TestCase):
    urls = 'sentry.tests.urls'

    def setUp(self):
        self._client = settings.CLIENT
        
    def tearDown(self):
        settings.CLIENT = self._client
    
    def test_get_client(self):
        from sentry.client.base import SentryClient
        from sentry.client.log import LoggingSentryClient
        self.assertEquals(get_client().__class__, SentryClient)
        self.assertEquals(get_client(), get_client())
    
        settings.CLIENT = 'sentry.client.log.LoggingSentryClient'
        
        self.assertEquals(get_client().__class__, LoggingSentryClient)
        self.assertEquals(get_client(), get_client())
    
        settings.CLIENT = 'sentry.client.base.SentryClient'
    
    def test_logging_client(self):
        settings.CLIENT = 'sentry.client.log.LoggingSentryClient'
        
        client = get_client()
        
        _foo = {'': None}
        
        class handler(logging.Handler):
            def emit(self, record):
                _foo[''] = record

        logger = client.logger
        logger.addHandler(handler())
        
        self.assertRaises(Exception, self.client.get, reverse('sentry-raise-exc'))
        
        self.assertEquals(_foo[''].getMessage(), 'view exception')
        self.assertEquals(_foo[''].levelno, client.default_level)
        self.assertEquals(_foo[''].class_name, 'Exception')

