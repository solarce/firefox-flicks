# -*- coding: utf-8 -*-
"""
    celery.backends.amqp
    ~~~~~~~~~~~~~~~~~~~~

    The AMQP result backend.

    This backend publishes results as messages.

"""
from __future__ import absolute_import
from __future__ import with_statement

import socket
import threading
import time

from kombu import Exchange, Queue, Producer, Consumer

from celery import states
from celery.exceptions import TimeoutError
from celery.utils.log import get_logger

from .base import BaseDictBackend

logger = get_logger(__name__)


class BacklogLimitExceeded(Exception):
    """Too much state history to fast-forward."""


def repair_uuid(s):
    # Historically the dashes in UUIDS are removed from AMQ entity names,
    # but there is no known reason to.  Hopefully we'll be able to fix
    # this in v4.0.
    return '%s-%s-%s-%s-%s' % (s[:8], s[8:12], s[12:16], s[16:20], s[20:])


class AMQPBackend(BaseDictBackend):
    """Publishes results by sending messages."""
    Exchange = Exchange
    Queue = Queue
    Consumer = Consumer
    Producer = Producer

    BacklogLimitExceeded = BacklogLimitExceeded

    supports_native_join = True

    retry_policy = {
        'max_retries': 20,
        'interval_start': 0,
        'interval_step': 1,
        'interval_max': 1,
    }

    def __init__(self, connection=None, exchange=None, exchange_type=None,
                 persistent=None, serializer=None, auto_delete=True,
                 **kwargs):
        super(AMQPBackend, self).__init__(**kwargs)
        conf = self.app.conf
        self._connection = connection
        self.queue_arguments = {}
        self.persistent = (conf.CELERY_RESULT_PERSISTENT if persistent is None
                           else persistent)
        delivery_mode = persistent and 'persistent' or 'transient'
        exchange = exchange or conf.CELERY_RESULT_EXCHANGE
        exchange_type = exchange_type or conf.CELERY_RESULT_EXCHANGE_TYPE
        self.exchange = self.Exchange(name=exchange,
                                      type=exchange_type,
                                      delivery_mode=delivery_mode,
                                      durable=self.persistent,
                                      auto_delete=False)
        self.serializer = serializer or conf.CELERY_RESULT_SERIALIZER
        self.auto_delete = auto_delete

        # AMQP_TASK_RESULT_EXPIRES setting is deprecated and will be
        # removed in version 4.0.
        dexpires = conf.CELERY_AMQP_TASK_RESULT_EXPIRES

        self.expires = None
        if 'expires' in kwargs:
            if kwargs['expires'] is not None:
                self.expires = self.prepare_expires(kwargs['expires'])
        else:
            self.expires = self.prepare_expires(dexpires)

        if self.expires:
            self.queue_arguments['x-expires'] = int(self.expires * 1000)
        self.mutex = threading.Lock()

    def _create_binding(self, task_id):
        name = task_id.replace('-', '')
        return self.Queue(name=name,
                          exchange=self.exchange,
                          routing_key=name,
                          durable=self.persistent,
                          auto_delete=self.auto_delete,
                          queue_arguments=self.queue_arguments)

    def revive(self, channel):
        pass

    def _republish(self, channel, task_id, body, content_type,
                   content_encoding):
        return Producer(channel).publish(
            body,
            exchange=self.exchange,
            routing_key=task_id.replace('-', ''),
            serializer=self.serializer,
            content_type=content_type,
            content_encoding=content_encoding,
            retry=True, retry_policy=self.retry_policy,
            declare=[self._create_binding(task_id)],
        )

    def _store_result(self, task_id, result, status, traceback=None):
        """Send task return value and status."""
        with self.mutex:
            with self.app.amqp.producer_pool.acquire(block=True) as pub:
                pub.publish({'task_id': task_id, 'status': status,
                             'result': self.encode_result(result, status),
                             'traceback': traceback,
                             'children': self.current_task_children()},
                            exchange=self.exchange,
                            routing_key=task_id.replace('-', ''),
                            serializer=self.serializer,
                            retry=True, retry_policy=self.retry_policy,
                            declare=[self._create_binding(task_id)])
        return result

    def wait_for(self, task_id, timeout=None, cache=True, propagate=True,
                 **kwargs):
        cached_meta = self._cache.get(task_id)
        if cache and cached_meta and \
                cached_meta['status'] in states.READY_STATES:
            meta = cached_meta
        else:
            try:
                meta = self.consume(task_id, timeout=timeout)
            except socket.timeout:
                raise TimeoutError('The operation timed out.')

        state = meta['status']
        if state == states.SUCCESS:
            return meta['result']
        elif state in states.PROPAGATE_STATES:
            if propagate:
                raise self.exception_to_python(meta['result'])
            return meta['result']
        else:
            return self.wait_for(task_id, timeout, cache)

    def get_task_meta(self, task_id, backlog_limit=1000):
        # Polling and using basic_get
        with self.app.pool.acquire_channel(block=True) as (_, channel):
            binding = self._create_binding(task_id)(channel)
            binding.declare()
            latest, acc = None, None
            for i in xrange(backlog_limit):
                latest, acc = acc, binding.get(no_ack=True)
                if not acc:  # no more messages
                    break
            else:
                raise self.BacklogLimitExceeded(task_id)

            if latest:
                # new state to report
                self._republish(channel, task_id, latest.body,
                                latest.content_type, latest.content_encoding)
                payload = self._cache[task_id] = latest.payload
                return payload
            else:
                # no new state, use previous
                try:
                    return self._cache[task_id]
                except KeyError:
                    # result probably pending.
                    return {'status': states.PENDING, 'result': None}
    poll = get_task_meta  # XXX compat

    def drain_events(self, connection, consumer, timeout=None, now=time.time):
        wait = connection.drain_events
        results = {}

        def callback(meta, message):
            if meta['status'] in states.READY_STATES:
                uuid = repair_uuid(message.delivery_info['routing_key'])
                results[uuid] = meta

        consumer.callbacks[:] = [callback]
        time_start = now()

        while 1:
            # Total time spent may exceed a single call to wait()
            if timeout and now() - time_start >= timeout:
                raise socket.timeout()
            wait(timeout=timeout)
            if results:  # got event on the wanted channel.
                break
        self._cache.update(results)
        return results

    def consume(self, task_id, timeout=None):
        with self.app.pool.acquire_channel(block=True) as (conn, channel):
            binding = self._create_binding(task_id)
            with self.Consumer(channel, binding, no_ack=True) as consumer:
                return self.drain_events(conn, consumer, timeout).values()[0]

    def get_many(self, task_ids, timeout=None, **kwargs):
        with self.app.pool.acquire_channel(block=True) as (conn, channel):
            ids = set(task_ids)
            cached_ids = set()
            for task_id in ids:
                try:
                    cached = self._cache[task_id]
                except KeyError:
                    pass
                else:
                    if cached['status'] in states.READY_STATES:
                        yield task_id, cached
                        cached_ids.add(task_id)
            ids ^= cached_ids

            bindings = [self._create_binding(task_id) for task_id in task_ids]
            with self.Consumer(channel, bindings, no_ack=True) as consumer:
                while ids:
                    r = self.drain_events(conn, consumer, timeout)
                    ids ^= set(r)
                    for ready_id, ready_meta in r.iteritems():
                        yield ready_id, ready_meta

    def reload_task_result(self, task_id):
        raise NotImplementedError(
            'reload_task_result is not supported by this backend.')

    def reload_group_result(self, task_id):
        """Reload group result, even if it has been previously fetched."""
        raise NotImplementedError(
            'reload_group_result is not supported by this backend.')

    def save_group(self, group_id, result):
        raise NotImplementedError(
            'save_group is not supported by this backend.')

    def restore_group(self, group_id, cache=True):
        raise NotImplementedError(
            'restore_group is not supported by this backend.')

    def delete_group(self, group_id):
        raise NotImplementedError(
            'delete_group is not supported by this backend.')

    def __reduce__(self, args=(), kwargs={}):
        kwargs.update(
            connection=self._connection,
            exchange=self.exchange.name,
            exchange_type=self.exchange.type,
            persistent=self.persistent,
            serializer=self.serializer,
            auto_delete=self.auto_delete,
            expires=self.expires,
        )
        return super(AMQPBackend, self).__reduce__(args, kwargs)
