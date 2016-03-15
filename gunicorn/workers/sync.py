# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.
#

from datetime import datetime
import errno
import os
import select
import socket
import ssl
import sys

import gunicorn.http as http
import gunicorn.http.wsgi as wsgi
import gunicorn.util as util
import gunicorn.workers.base as base
from gunicorn import six

class StopWaiting(Exception):
    """ exception raised to stop waiting for a connnection """

class SyncWorker(base.Worker):

    def accept(self, listener):
        #接收客户端的socket 数据
        client, addr = listener.accept()
        #设置成阻塞模式
        client.setblocking(1)
        #然后还要加锁
        util.close_on_exec(client)
        #处理这个请求
        self.handle(listener, client, addr)

    def wait(self, timeout):
        try:
            self.notify()
            ret = select.select(self.sockets, [], self.PIPE, timeout)
            if ret[0]:
                return ret[0]

        except select.error as e:
            if e.args[0] == errno.EINTR:
                return self.sockets
            if e.args[0] == errno.EBADF:
                if self.nr < 0:
                    return self.sockets
                else:
                    raise StopWaiting
            raise

    def is_parent_alive(self):
        # If our parent changed then we shut down.
        if self.ppid != os.getppid():
            self.log.info("Parent changed, shutting down: %s", self)
            return False
        return True

    def run_for_one(self, timeout):
        #只有一个监听socket

        listener = self.sockets[0]
        while self.alive:
            self.notify()

            # Accept a connection. If we get an error telling us
            # that no connection is waiting we fall down to the
            # select which is where we'll wait for a bit for new
            # workers to come give us some love.
            try:
                #接收并处理客户端的socket连接
                self.accept(listener)
                # Keep processing clients until no one is waiting. This
                # prevents the need to select() for every client that we
                # process.
                continue

            except EnvironmentError as e:
                if e.errno not in (errno.EAGAIN, errno.ECONNABORTED,
                        errno.EWOULDBLOCK):
                    raise

            #保证父进程还活着
            if not self.is_parent_alive():
                return

            try:
                self.wait(timeout)
            except StopWaiting:
                return

    def run_for_multiple(self, timeout):
        while self.alive:
            self.notify()

            try:
                ready = self.wait(timeout)
            except StopWaiting:
                return

            if ready is not None:
                for listener in ready:
                    try:
                        self.accept(listener)
                    except EnvironmentError as e:
                        if e.errno not in (errno.EAGAIN, errno.ECONNABORTED,
                                errno.EWOULDBLOCK):
                            raise

            if not self.is_parent_alive():
                return

    def run(self):
        # 子程序初始化完成之后，这里就是主循环

        # if no timeout is given the worker will never wait and will
        # use the CPU for nothing. This minimal timeout prevent it.
        timeout = self.timeout or 0.5

        # self.socket appears to lose its blocking status after
        # we fork in the arbiter. Reset it here.
        self.log.info("worker sockets: %s"%self.sockets)
        for s in self.sockets:
            s.setblocking(0)

        if len(self.sockets) > 1:
            self.run_for_multiple(timeout)
        else:
            self.run_for_one(timeout)

    def handle(self, listener, client, addr):
        #主要处理http请求的地方
        #listener 是server的fd
        #client 是client的fd
        #addr 是client的地址

        req = None
        try:
            #处理ssl请求
            if self.cfg.is_ssl:
                client = ssl.wrap_socket(client, server_side=True,
                    **self.cfg.ssl_options)

            # 生成request对象
            # 交给handle_request 处理
            parser = http.RequestParser(self.cfg, client)
            req = six.next(parser)


            self.handle_request(listener, req, client, addr)
        except http.errors.NoMoreData as e:
            self.log.debug("Ignored premature client disconnection. %s", e)
        except StopIteration as e:
            self.log.debug("Closing connection. %s", e)
        except ssl.SSLError as e:
            if e.args[0] == ssl.SSL_ERROR_EOF:
                self.log.debug("ssl connection closed")
                client.close()
            else:
                self.log.debug("Error processing SSL request.")
                self.handle_error(req, client, addr, e)
        except EnvironmentError as e:
            if e.errno not in (errno.EPIPE, errno.ECONNRESET):
                self.log.exception("Socket error processing request.")
            else:
                if e.errno == errno.ECONNRESET:
                    self.log.debug("Ignoring connection reset")
                else:
                    self.log.debug("Ignoring EPIPE")
        except Exception as e:
            self.handle_error(req, client, addr, e)
        finally:
            util.close(client)

    def handle_request(self, listener, req, client, addr):
        environ = {}
        resp = None
        try:
            #server hooks
            self.cfg.pre_request(self, req)
            request_start = datetime.now()

            #记录日志
            self.log.info("%s %s %s %s"%(req, client, addr, listener.getsockname()))

            #使用 http.wsgi.create 来组织request和response对象
            #准备好wsgi需要的对象
            resp, environ = wsgi.create(req, client, addr,
                    listener.getsockname(), self.cfg)

            #self.log.info("%s %s"%(resp, environ))
            # Force the connection closed until someone shows
            # a buffering proxy that supports Keep-Alive to
            # the backend.
            resp.force_close()
            self.nr += 1

            #处理一定量的request之后会自动重启worker
            if self.nr >= self.max_requests:
                self.log.info("Autorestarting worker after current request.")
                self.alive = False

            #wsgi 处理请求， 不同类型的mime分别响应
            #base.py 中load_wsgi
            respiter = self.wsgi(environ, resp.start_response)
            self.log.info(respiter)

            try:
                if isinstance(respiter, environ['wsgi.file_wrapper']):
                    resp.write_file(respiter)
                else:
                    for item in respiter:
                        resp.write(item)
                resp.close()

                #计算出整个请求的花费时间
                request_time = datetime.now() - request_start

                #类似nginx 记录access log
                self.log.access(resp, req, environ, request_time)
            finally:
                if hasattr(respiter, "close"):
                    respiter.close()
        except EnvironmentError:
            exc_info = sys.exc_info()
            # pass to next try-except level
            six.reraise(exc_info[0], exc_info[1], exc_info[2])
        except Exception:
            if resp and resp.headers_sent:
                # If the requests have already been sent, we should close the
                # connection to indicate the error.
                self.log.exception("Error handling request")
                try:
                    client.shutdown(socket.SHUT_RDWR)
                    client.close()
                except EnvironmentError:
                    pass
                raise StopIteration()
            raise
        finally:
            try:
                self.cfg.post_request(self, req, environ, resp)
            except Exception:
                self.log.exception("Exception in post_request hook")
