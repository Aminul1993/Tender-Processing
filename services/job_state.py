"""
The processing job's state in Redis, shared by the web app and any number of worker processes (worker.py), so
several workers can process the tenders of one job at the same time. Without REDIS_URL the state is kept in an
in-process Redis (fakeredis): one process, as before, and lost on restart.

Keys, each under the prefix (REDIS_PREFIX):
  job          hash: job_id, status (running / completed), started_at, finished_at, stop_requested ('1' or '')
  order        list: the job's item IDs, in the order they were queued
  items        hash: item ID -> the item as JSON: {'id', 'tcno', 'name', 'status', 'rechecked', ...SUMMARY_KEYS}
  payloads     hash: item ID -> what a worker needs to process it, as JSON: {'tender', 'regenerate', 'approve_ocr'}
  queue        list: the IDs of the items waiting, oldest first
  wake         list: a token per item queued, so an idle worker blocked on it starts at once (workers also poll)
  stages       hash: item ID -> {'stage', 'step', 'steps', 'started'} of an item being processed
  lease:<id>   expires LEASE_SECONDS after the worker processing the item last renewed it: gone, the worker is
               taken as dead and the item failed (see TenderProcessor.progress())
  results      hash: TCNO -> the tender's last full result as JSON (also saved in its folder, which is the record)
  briefs       hash: TCNO -> the fields of it the tender table, the run estimates and the folder scan need
  seq          the last item ID given
  lock         the lock every change is made under: held for milliseconds, never while a tender is processed

Every write is made under lock(); reads are not, and see each key as of one moment.
"""
import json
import time
import uuid
from contextlib import contextmanager

from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.exceptions import WatchError

LEASE_SECONDS = 90
# Seconds the Redis client waits for a reply, comfortably more than wait() blocks for (5)
SOCKET_TIMEOUT = 30
# The job lock: held for milliseconds, it expires after LOCK_SECONDS should its holder die, and is waited for up to
# LOCK_WAIT_SECONDS
LOCK_SECONDS = 60
LOCK_WAIT_SECONDS = 120
# The keys of the job; the results, briefs and item ID sequence outlive it
JOB_KEYS = ('job', 'order', 'items', 'payloads', 'queue', 'wake', 'stages')


class JobState:
    """The job, its items and the tenders' results, in redis (a client made with decode_responses=True)."""

    def __init__(self, redis, prefix='tender-processing:'):
        self.redis = redis
        self.prefix = prefix

    @classmethod
    def connect(cls, url=None, prefix='tender-processing:'):
        """The state in the Redis at url (redis://...), or in an in-process Redis without one."""
        if url:
            import redis
            # redis-py 8 gives up on a reply after 5 seconds by default, no longer than wait() blocks for
            return cls(redis.Redis.from_url(url, decode_responses=True, health_check_interval=30,
                                            socket_timeout=SOCKET_TIMEOUT, socket_connect_timeout=10), prefix)
        import fakeredis
        return cls(fakeredis.FakeRedis(decode_responses=True), prefix)

    def _key(self, name):
        return f'{self.prefix}{name}'

    @contextmanager
    def lock(self):
        """
        The lock to change the state under (see the module docstring). It expires after LOCK_SECONDS, should its holder
        die. Taken with SET NX and released in a WATCH transaction rather than a Lua script, so it works on any
        Redis-compatible server. Raises TimeoutError when it cannot be taken within LOCK_WAIT_SECONDS.
        """
        key, token = self._key('lock'), uuid.uuid4().hex
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while not self.redis.set(key, token, nx=True, px=LOCK_SECONDS * 1000):
            if time.monotonic() > deadline:
                raise TimeoutError(f'Could not take the job lock {key} in {LOCK_WAIT_SECONDS} seconds')
            time.sleep(0.01)
        try:
            yield
        finally:
            # Deleted only while still this holder's: had it expired, another may hold it now
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    if pipe.get(key) == token:
                        pipe.multi()
                        pipe.delete(key)
                        pipe.execute()
                except WatchError:
                    pass

    # ---------- the job ----------

    def job(self):
        """The job as {'job_id', 'status', 'started_at', 'finished_at', 'stop_requested'}, or None before any."""
        job = self.redis.hgetall(self._key('job'))
        if not job:
            return None
        return {'job_id': job['job_id'], 'status': job['status'], 'started_at': job['started_at'],
                'finished_at': job.get('finished_at') or None, 'stop_requested': job.get('stop_requested') == '1'}

    def new_job(self, job_id, started_at):
        """Starts job_id, empty, in place of the last job."""
        pipe = self.redis.pipeline()
        pipe.delete(*(self._key(name) for name in JOB_KEYS))
        pipe.hset(self._key('job'), mapping={'job_id': job_id, 'status': 'running', 'started_at': started_at,
                                             'finished_at': '', 'stop_requested': ''})
        pipe.execute()

    def clear_job(self):
        """Forgets the job, as if none had run; the results stay."""
        self.redis.delete(*(self._key(name) for name in JOB_KEYS))

    def update_job(self, **fields):
        """Sets fields of the job; True is stored as '1', False and None as ''."""
        values = {name: '1' if value is True else '' if value in (False, None) else value
                  for name, value in fields.items()}
        self.redis.hset(self._key('job'), mapping=values)

    # ---------- items ----------

    def items(self):
        """The job's items, in order."""
        ids = self.redis.lrange(self._key('order'), 0, -1)
        values = self.redis.hmget(self._key('items'), ids) if ids else []
        return [json.loads(value) for value in values if value]

    def item(self, item_id):
        value = self.redis.hget(self._key('items'), item_id)
        return json.loads(value) if value else None

    def put_item(self, item):
        self.redis.hset(self._key('items'), item['id'], json.dumps(item))

    def add_item(self, item, payload):
        """Adds item to the job and queues it, with the payload a worker processes it from; returns its ID."""
        item = {**item, 'id': str(self.redis.incr(self._key('seq')))}
        pipe = self.redis.pipeline()
        pipe.hset(self._key('items'), item['id'], json.dumps(item))
        pipe.hset(self._key('payloads'), item['id'], json.dumps(payload))
        pipe.rpush(self._key('order'), item['id'])
        pipe.rpush(self._key('queue'), item['id'])
        pipe.rpush(self._key('wake'), item['id'])
        pipe.execute()
        return item['id']

    def payload(self, item_id):
        value = self.redis.hget(self._key('payloads'), item_id)
        return json.loads(value) if value else None

    def next_queued(self):
        """Takes the ID of the oldest item waiting off the queue, or None."""
        return self.redis.lpop(self._key('queue'))

    def unqueue(self, item_ids):
        """Takes these items off the queue (cancelled)."""
        pipe = self.redis.pipeline()
        for item_id in item_ids:
            pipe.lrem(self._key('queue'), 0, item_id)
        pipe.execute()

    def remove_items(self, item_ids):
        """Takes these items out of the job altogether: the items, their payloads, places in it, stages and leases."""
        if not item_ids:
            return
        pipe = self.redis.pipeline()
        for name in ('items', 'payloads', 'stages'):
            pipe.hdel(self._key(name), *item_ids)
        for item_id in item_ids:
            pipe.lrem(self._key('order'), 0, item_id)
            pipe.lrem(self._key('queue'), 0, item_id)
            pipe.delete(self._key(f'lease:{item_id}'))
        pipe.execute()

    def wait(self, timeout):
        """
        Blocks until an item is queued, or for timeout seconds (whole, at least 1), and always for less than the
        client waits for a reply (its socket timeout, which the URL may set), so the wait is not taken for a dead
        connection. A wait that times out all the same returns as if nothing was queued: workers poll anyway.
        """
        limit = self.redis.connection_pool.connection_kwargs.get('socket_timeout')
        seconds = max(1, round(timeout))
        if limit:
            seconds = max(1, min(seconds, int(limit) - 2))
        try:
            self.redis.blpop([self._key('wake')], timeout=seconds)
        except RedisTimeoutError:
            pass

    # ---------- items being processed ----------

    def set_stage(self, item_id, stage, step=None, steps=None, started=None):
        self.redis.hset(self._key('stages'), item_id, json.dumps(
            {'stage': stage, 'step': step, 'steps': steps, 'started': started or time.time()}))

    def stages(self):
        return {item_id: json.loads(value) for item_id, value in self.redis.hgetall(self._key('stages')).items()}

    def renew(self, item_id, worker):
        """Keeps the item leased to worker for LEASE_SECONDS more."""
        self.redis.set(self._key(f'lease:{item_id}'), worker, ex=LEASE_SECONDS)

    def leased(self, item_ids):
        """Those of item_ids whose lease is alive."""
        pipe = self.redis.pipeline()
        for item_id in item_ids:
            pipe.exists(self._key(f'lease:{item_id}'))
        return {item_id for item_id, alive in zip(item_ids, pipe.execute()) if alive}

    def release(self, item_id):
        """Forgets the stage and lease of an item no longer processed."""
        pipe = self.redis.pipeline()
        pipe.hdel(self._key('stages'), item_id)
        pipe.delete(self._key(f'lease:{item_id}'))
        pipe.execute()

    # ---------- results ----------

    def result(self, tcno):
        value = self.redis.hget(self._key('results'), str(tcno))
        return json.loads(value) if value else None

    def results(self):
        return [json.loads(value) for value in self.redis.hvals(self._key('results'))]

    def briefs(self):
        """{TCNO: brief} of every result (see put_result())."""
        return {tcno: json.loads(value) for tcno, value in self.redis.hgetall(self._key('briefs')).items()}

    def put_result(self, result, brief):
        pipe = self.redis.pipeline()
        pipe.hset(self._key('results'), str(result['tcno']), json.dumps(result, ensure_ascii=False))
        pipe.hset(self._key('briefs'), str(result['tcno']), json.dumps(brief, ensure_ascii=False))
        pipe.execute()

    def delete_result(self, tcno):
        pipe = self.redis.pipeline()
        pipe.hdel(self._key('results'), str(tcno))
        pipe.hdel(self._key('briefs'), str(tcno))
        pipe.execute()

    def replace_results(self, results, briefs):
        """Replaces every result (and brief) with these, {TCNO: result} and {TCNO: brief}."""
        pipe = self.redis.pipeline()
        pipe.delete(self._key('results'), self._key('briefs'))
        if results:
            pipe.hset(self._key('results'), mapping={tcno: json.dumps(result, ensure_ascii=False)
                                                     for tcno, result in results.items()})
            pipe.hset(self._key('briefs'), mapping={tcno: json.dumps(brief, ensure_ascii=False)
                                                    for tcno, brief in briefs.items()})
        pipe.execute()
