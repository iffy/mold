from twisted.trial.unittest import TestCase
from twisted.test.proto_helpers import StringTransport
from twisted.internet import reactor
from twisted.python.filepath import FilePath


import json
import os


from mold.process import LoggingProtocol, spawnLogged, NetstringBuffer
from mold.log import MessageFactory



class NetstringBufferTest(TestCase):


    def test_whole(self):
        """
        Whole netstrings are allowed
        """
        called = []
        n = NetstringBuffer(called.append)
        n.dataReceived('3:foo,')
        self.assertEqual(called, ['foo'])


    def test_two(self):
        """
        Two netstrings are allowed
        """
        called = []
        n = NetstringBuffer(called.append)
        n.dataReceived('3:foo,4:foot,')
        self.assertEqual(called, ['foo', 'foot'])


    def test_invalid(self):
        """
        Invalid netstrings are sent to the error collector
        """
        c1 = []
        c2 = []
        n = NetstringBuffer(c1.append, c2.append)
        n.dataReceived('foobar2:ok,something3:foohello9')
        self.assertEqual(c1, ['ok'])
        self.assertEqual(c2, ['foobar', 'something', '3:foohello'])


    def test_leading0(self):
        c1 = []
        c2 = []
        n = NetstringBuffer(c1.append, c2.append)
        n.dataReceived('02:ok,0:,')
        self.assertEqual(c1, ['ok', ''])
        self.assertEqual(c2, ['0'])


    def test_partial(self):
        """
        You can get the string a part at a time
        """
        c1 = []
        c2 = []
        n = NetstringBuffer(c1.append, c2.append)
        n.dataReceived('3:a')
        self.assertEqual(c1, [])
        self.assertEqual(c2, [])
        n.dataReceived('bc')
        self.assertEqual(c1, [])
        self.assertEqual(c2, [])
        n.dataReceived(',')
        self.assertEqual(c1, ['abc'])
        self.assertEqual(c2, [])


    def test_partial_length(self):
        """
        You can get a string a piece at a time, even the length
        """
        c1 = []
        c2 = []
        n = NetstringBuffer(c1.append, c2.append)
        
        msg = 'a' * 8920
        s = '%s:%s' % (len(msg), msg)
        for c in s:
            n.dataReceived(c)
            self.assertEqual(c1, [])
            self.assertEqual(c2, [])
        n.dataReceived(',')
        self.assertEqual(c1, [msg])
        self.assertEqual(c2, [])



class spawnLoggedTest(TestCase):


    timeout = 1


    def test_functional(self):
        """
        Spawn should actually execute the program and return the expected
        output.
        """
        script = FilePath(self.mktemp())
        script.setContent('echo "stdout"\n'
                          'echo "stderr" >&2\n'
                          'echo "control" >&3\n')
        
        called = []
        proto = LoggingProtocol(stdin='theinput', control=called.append)
        
        spawnLogged(reactor, proto, '/bin/bash', ['bash', script.path],
              {'FOO':'BAR'}, '/tmp', usePTY=False)
        
        def check(result):
            code, sig = result
            self.assertEqual(code, 0)
            self.assertEqual(sig, None)
            
            # assert logging happened
            start_msg = proto.msg_factory.processSpawned(proto.label,
                '/bin/bash', ['bash', script.path],
                {'FOO':'BAR'}, '/tmp', os.geteuid(), os.getegid(), False)
            self.assertIn(start_msg, called, 'Expected:\n%r\n\nin:\n%s' % (
                            start_msg, '\n'.join(called)))
            
            msg = proto.msg_factory.fd(proto.label, 0, 'theinput')
            self.assertIn(msg, called, "Should have logged the input")
            
            msg = proto.msg_factory.fd(proto.label, 1, 'stdout\n')
            self.assertIn(msg, called, "Should have logged stdout")
            
            msg = proto.msg_factory.fd(proto.label, 2, 'stderr\n')
            self.assertIn(msg, called, "Should have logged stderr")
            
            self.assertIn('control\n', called, "Control should go right through:"
                                       "\n%s\n\n%s" % (msg, '\n'.join(called)))
        
        return proto.done.addCallback(check)



class LoggingProtocolTest(TestCase):


    timeout = 1


    def test_defaults(self):
        """
        The LoggingProtocol should generate a uuid.
        """
        proto = LoggingProtocol()
        self.assertNotEqual(proto.label, None)
        self.assertTrue(isinstance(proto.msg_factory, MessageFactory))
        

    def test_init(self):
        """
        L{LoggingProtocol} should accept a label.
        """
        f = MessageFactory()
        proto = LoggingProtocol(label='foo', msg_factory=f)
        self.assertEqual(proto.label, 'foo')
        self.assertEqual(proto.msg_factory, f)


    def test_done(self):
        """
        The protocol should have a C{done} Deferred which fires with the exit
        code when it's done.
        """
        proto = LoggingProtocol()    
        self.assertFalse(proto.done.called)        
        
        # fake control receiver
        called = []
        proto.sendControl = called.append
        
        reactor.spawnProcess(proto, '/bin/echo', ['echo', 'foo'])
        
        def check(response):
            exitcode, signal = response
            self.assertEqual(exitcode, 0)
            self.assertEqual(signal, None)
            
            self.assertEqual(called[-1],
                proto.msg_factory.processEnded(proto.label, 0, None))
            
        return proto.done.addCallback(check)


    def test_done_error(self):
        """
        The protocol should report any signal used on the process
        """
        script = FilePath(self.mktemp())
        script.setContent('sleep 500')
               
        proto = LoggingProtocol()

        # fake control receiver
        called = []
        proto.sendControl = called.append
        
        # kill the process as soon as it's connected
        def connectionMade():
            proto.transport.signalProcess(9)
        proto.connectionMade = connectionMade

        reactor.spawnProcess(proto, '/bin/bash', ['bash', script.path])
        
        def cb(response):
            self.fail('Should have errbacked: %r' % (response,))
        
        def eb(response):
            self.assertNotEqual(response.value.exitCode, 0)
            self.assertEqual(response.value.signal, 9)
            
            self.assertEqual(called[-1],
                proto.msg_factory.processEnded(proto.label, None, 9))
        
        return proto.done.addCallbacks(cb, eb)


    def test_childDataReceived(self):
        """
        Depending on the channel, the corresponding method should be called.
        """
        called = []
        proto = LoggingProtocol()
        
        proto.outReceived = lambda x:called.append(('out', x))
        proto.errReceived = lambda x:called.append(('err', x))
        proto.ctlReceived = lambda x:called.append(('ctl', x))
        
        proto.childDataReceived(1, 'foo')
        self.assertEqual(called, [('out', 'foo')], "1 = stdout")
        
        called.pop()
        proto.childDataReceived(2, 'bar')
        self.assertEqual(called, [('err', 'bar')], "2 = stderr")
        
        called.pop()
        proto.childDataReceived(3, 'wow')
        self.assertEqual(called, [('ctl', 'wow')], "3 = control")


    def test_sendControl(self):
        """
        Should have no side effects by default
        """
        proto = LoggingProtocol()
        proto.sendControl('foo')


    def test_sendControl_data(self):
        """
        If there's a control handler, pass messages to it.
        """
        called = []
        
        proto = LoggingProtocol(control=called.append)
        proto.sendControl('foo')
        self.assertEqual(called, ['foo'])
        proto.sendControl('bar')
        self.assertEqual(called, ['foo', 'bar'])


    def test_ctlReceived_invalid(self):
        """
        If an invalid netstring is sent on the control signal, send the raw
        data.
        """
        called = []
        
        proto = LoggingProtocol(control=called.append)
        proto.ctlReceived('foo')
        
        self.assertEqual(called[0], proto.msg_factory.fd(proto.label, 3, 'foo'))


    def test_ctlReceived_whole(self):
        """
        You can pass a whole netstring on the control descriptor.
        """
        called = []
        
        proto = LoggingProtocol(control=called.append)
        proto.ctlReceived('3:foo,')
        
        self.assertEqual(called[0], '3:foo,')

    def test_outReceived_default(self):
        """
        By default, all output is sent to the historian, marked as "out".
        """
        called = []
        
        proto = LoggingProtocol()
        proto.sendControl = called.append
        
        proto.outReceived('foo')
        
        self.assertEqual(called[0],
                proto.msg_factory.fd(proto.label, 1, 'foo'))


    def test_outReceived_passthru(self):
        """
        You can ask the protocol for all stdout.
        """
        called = []
        
        proto = LoggingProtocol(stdout=called.append)
        proto.outReceived('foo')
        self.assertEqual(called, ['foo'], "Should have sent stdout")


    def test_errReceived_default(self):
        """
        By default, all stderr is sent to the historian, marked as "err".
        """
        called = []
        
        proto = LoggingProtocol()
        proto.sendControl = called.append
        
        proto.errReceived('foo')
        
        self.assertEqual(called[0],
                proto.msg_factory.fd(proto.label, 2, 'foo'))


    def test_errReceived_passthru(self):
        """
        You can ask the protocol for all stderr.
        """
        called = []
        
        proto = LoggingProtocol(stderr=called.append)
        proto.errReceived('foo')
        self.assertEqual(called, ['foo'], "Should have sent stderr")


    def test_stdin_on_init(self):
        """
        You can pass a string to be sent on stdin as soon as the protocol is
        connected.
        """
        transport = StringTransport()
        
        proto = LoggingProtocol(stdin='foo')
        
        written = []
        proto.write = written.append
        
        called = []
        proto.closeStdin = lambda: called.append('closeStdin')
        
        proto.makeConnection(transport)
        
        self.assertEqual(written, ['foo'], "Should write to stdin")
        self.assertEqual(called, ['closeStdin'], "Should have closed stdin")


    def test_write(self):
        """
        You can write straight to stdin.
        """
        called = []
        transport = StringTransport()
        transport.closeStdin = lambda: called.append('closeStdin')
        
        proto = LoggingProtocol()
        proto.makeConnection(transport)
        
        self.assertEqual(transport.value(), '', "Should not write to stdin")
        self.assertEqual(called, [], "Should not close stdin")
        
        proto.write('foo')
        self.assertEqual(transport.value(), 'foo')
        self.assertEqual(called, [], "Should not close stdin")

        proto.write('bar')
        self.assertEqual(transport.value(), 'foobar')
        self.assertEqual(called, [], "Should not close stdin")

        proto.closeStdin()
        self.assertEqual(called, ['closeStdin'], "Should close stdin")


    def test_write_goes_to_control(self):
        """
        Things written to stdin are also written to control.
        """
        transport = StringTransport()
        
        proto = LoggingProtocol()
        proto.makeConnection(transport)

        called = []
        proto.sendControl = called.append
        
        proto.write('foo')
        self.assertEqual(called[0], proto.msg_factory.fd(proto.label, 0, 'foo'))


    def test_logSpawn(self):
        """
        You can log that a spawn happened.
        """       
        proto = LoggingProtocol()
        
        called = []
        proto.sendControl = called.append
        
        proto.logSpawn('/bin/bash', ['bash', 'foo'], {'FOO':'BAR'},
                       '/tmp', 'user1', 'group1', usePTY=False)
        self.assertEqual(called[0], proto.msg_factory.processSpawned(
                       proto.label,
                       '/bin/bash', ['bash', 'foo'], {'FOO':'BAR'},
                       '/tmp', 'user1', 'group1', usePTY=False))


    def test_logSpawn_defaults(self):
        """
        If you don't pass certain params, these defaults are used.
        """
        proto = LoggingProtocol()
        
        called = []
        proto.sendControl = called.append
        
        proto.logSpawn('/bin/bash')
        self.assertEqual(called[0], proto.msg_factory.processSpawned(
                       proto.label,
                       '/bin/bash', (), {}, os.path.abspath(os.curdir),
                       os.geteuid(), os.getegid(), False))
        

