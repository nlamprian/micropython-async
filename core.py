try:
    import utime as time
except ImportError:
    import time
import utimeq
import logging


DEBUG = 0

log = logging.getLogger("asyncio")

type_gen = type((lambda: (yield))())

class EventLoop:

    def __init__(self, len=42):
        # lpqlen encoded in len to avoid modifying __init__.py
        # I'm lazy and want to maintain one file only.
        lpqlen = len >> 16
        qlen = len & 0xffff
        self.q = utimeq.utimeq(qlen)
        self.lpq = utimeq.utimeq(lpqlen)
        self._max_overdue_ms = 0
        self.hpq = None

    def time(self):
        return time.ticks_ms()

    def create_task(self, coro):
        # CPython 3.4.2
        self.call_later_ms_(0, coro)
        # CPython asyncio incompatibility: we don't return Task object

    def max_overdue_ms(self, t=None):
        if t is not None:
            self._max_overdue_ms = t
        return self._max_overdue_ms

    def call_after_ms_(self, delay, callback, args=()):
        # low priority.
        t = time.ticks_add(self.time(), delay)
        if __debug__ and DEBUG:
            log.debug("Scheduling LP %s", (time, callback, args))
        self.lpq.push(t, callback, args)

    def call_after(self, delay, callback, *args):
        # low priority.
        t = time.ticks_add(self.time(), int(delay * 1000))
        if __debug__ and DEBUG:
            log.debug("Scheduling LP %s", (time, callback, args))
        self.lpq.push(t, callback, args)

    def _schedule_hp(self, func, callback, args=()):
        if self.hpq is None:
            self.hpq = [func, callback, args]
        else:  # Try to assign without allocation
            for entry in self.hpq:
                if not entry[0]:
                    entry[0] = func
                    entry[1] = callback
                    entry[2] = args
                    break
            else:
                self.hpq.append([func, callback, args])

    def allocate_hpq(self, size):  # Optionally pre-allocate HP queue
        if self.hpq is None:
            self.hpq = []
        for _ in range(size - len(self.hpq)):
            self.hpq.append([0, 0, 0])

    def call_soon(self, callback, *args):
        self.call_at(self.time(), callback, *args)

    def call_later(self, delay, callback, *args):
        self.call_at(time.ticks_add(self.time(), int(delay * 1000)), callback, *args)

    def call_later_ms_(self, delay, callback, args=()):
        self.call_at_(time.ticks_add(self.time(), delay), callback, args)

    def call_at(self, time, callback, *args):
        if __debug__ and DEBUG:
            log.debug("Scheduling %s", (time, callback, args))
        self.q.push(time, callback, args)

    def call_at_(self, time, callback, args=()):
        if __debug__ and DEBUG:
            log.debug("Scheduling %s", (time, callback, args))
        self.q.push(time, callback, args)

    def wait(self, delay):
        # Default wait implementation, to be overriden in subclasses
        # with IO scheduling
        if __debug__ and DEBUG:
            log.debug("Sleeping for: %s", delay)
        time.sleep_ms(delay)

    def run_forever(self):
        cur_task = [0, 0, 0]
        while True:
            if self.q:
                # wait() may finish prematurely due to I/O completion,
                # and schedule new, earlier than before tasks to run.
                while 1:
                    # Check high priority queue
                    if self.hpq is not None:
                        hp_found = False
                        for entry in self.hpq:
                            if entry[0] and entry[0]():
                                hp_found = True
                                entry[0] = 0
                                cur_task[0] = 0
                                cur_task[1] = entry[1] # ??? quick non-allocating copy
                                cur_task[2] = entry[2]
                                break
                        if hp_found:
                            break
                    # Schedule most overdue LP coro
                    tnow = self.time()
                    if self.lpq and self._max_overdue_ms > 0:
                        t = self.lpq.peektime()
                        overdue = -time.ticks_diff(t, tnow)
                        if overdue > self._max_overdue_ms:
                            self.lpq.pop(cur_task)
                            break
                    # Schedule any due normal task
                    t = self.q.peektime()
                    delay = time.ticks_diff(t, tnow)
                    if delay <= 0:
                        self.q.pop(cur_task)
                        break
                    # Schedule any due LP task
                    if self.lpq:
                        t = self.lpq.peektime()
                        lpdelay = time.ticks_diff(t, tnow)
                        if lpdelay <= 0:
                            self.lpq.pop(cur_task)
                            break
                        delay = min(delay, lpdelay)
                    self.wait(delay)  # superclass
                t = cur_task[0]
                cb = cur_task[1]
                args = cur_task[2]
                if __debug__ and DEBUG:
                    log.debug("Next coroutine to run: %s", (t, cb, args))
#                __main__.mem_info()
            else:
                ready = False
                if self.lpq:
                    t = self.lpq.peektime()
                    delay = time.ticks_diff(t, self.time())
                    if delay <= 0:
                        self.lpq.pop(cur_task)
                        t = cur_task[0]
                        cb = cur_task[1]
                        args = cur_task[2]
                        if __debug__ and DEBUG:
                            log.debug("Next coroutine to run: %s", (t, cb, args))
                        ready = True
                if not ready:
                    self.wait(-1)
                    # Assuming IO completion scheduled some tasks
                    continue
            if callable(cb):
                cb(*args)
            else:
                delay = 0
                func = None
                priority = True
                try:
                    if __debug__ and DEBUG:
                        log.debug("Coroutine %s send args: %s", cb, args)
                    if args == ():
                        ret = next(cb)
                    else:
                        ret = cb.send(*args)
                    if __debug__ and DEBUG:
                        log.debug("Coroutine %s yield result: %s", cb, ret)
                    if isinstance(ret, SysCall1):
                        arg = ret.arg
                        if isinstance(ret, AfterMs):
                            priority = False
                        if isinstance(ret, Sleep) or isinstance(ret, After):
                            delay = int(arg * 1000)
                        elif isinstance(ret, When):
                            if callable(arg):
                                func = arg
                            else:
                                assert False, "Argument to 'when' must be a function or method."
                        elif isinstance(ret, SleepMs):
                            delay = arg
                        elif isinstance(ret, IORead):
#                            self.add_reader(ret.obj.fileno(), lambda self, c, f: self.call_soon(c, f), self, cb, ret.obj)
#                            self.add_reader(ret.obj.fileno(), lambda c, f: self.call_soon(c, f), cb, ret.obj)
#                            self.add_reader(arg.fileno(), lambda cb: self.call_soon(cb), cb)
                            self.add_reader(arg, cb)
                            continue
                        elif isinstance(ret, IOWrite):
#                            self.add_writer(arg.fileno(), lambda cb: self.call_soon(cb), cb)
                            self.add_writer(arg, cb)
                            continue
                        elif isinstance(ret, IOReadDone):
                            self.remove_reader(arg)
                        elif isinstance(ret, IOWriteDone):
                            self.remove_writer(arg)
                        elif isinstance(ret, StopLoop):
                            return arg
                        else:
                            assert False, "Unknown syscall yielded: %r (of type %r)" % (ret, type(ret))
                    elif isinstance(ret, type_gen):
                        self.call_soon(ret)
                    elif isinstance(ret, int):
                        # Delay
                        delay = ret
                    elif ret is None:
                        # Just reschedule
                        pass
                    else:
                        assert False, "Unsupported coroutine yield value: %r (of type %r)" % (ret, type(ret))
                except StopIteration as e:
                    if __debug__ and DEBUG:
                        log.debug("Coroutine finished: %s", cb)
                    continue
                if func is not None:
                    self._schedule_hp(func, cb, args)
                else:
                    if priority:
                        self.call_later_ms_(delay, cb, args)
                    else:
                        self.call_after_ms_(delay, cb, args)

    def run_until_complete(self, coro):
        def _run_and_stop():
            yield from coro
            yield StopLoop(0)
        self.call_soon(_run_and_stop())
        self.run_forever()

    def close(self):
        pass


class SysCall:

    def __init__(self, *args):
        self.args = args

    def handle(self):
        raise NotImplementedError

# Optimized syscall with 1 arg
class SysCall1(SysCall):

    def __init__(self, arg):
        self.arg = arg

class StopLoop(SysCall1):
    pass

class IORead(SysCall1):
    pass

class IOWrite(SysCall1):
    pass

class IOReadDone(SysCall1):
    pass

class IOWriteDone(SysCall1):
    pass

_event_loop = None
_event_loop_class = EventLoop
def get_event_loop(qlen=42, lpqlen=42):
    global _event_loop
    if _event_loop is None:
        # Compatibility with official __init__.py: pack into 1 word
        _event_loop = _event_loop_class(qlen + (lpqlen << 16))
    return _event_loop

def sleep(secs):
    yield int(secs * 1000)

# Implementation of sleep_ms awaitable with zero heap memory usage
class SleepMs(SysCall1):

    def __init__(self):
        self.v = None
        self.arg = None

    def __call__(self, arg):
        self.v = arg
        #print("__call__")
        return self

    def __iter__(self):
        #print("__iter__")
        return self

    def __next__(self):
        if self.v is not None:
            #print("__next__ syscall enter")
            self.arg = self.v
            self.v = None
            return self
        #print("__next__ syscall exit")
        _stop_iter.__traceback__ = None
        raise _stop_iter

_stop_iter = StopIteration()
sleep_ms = SleepMs()

class Sleep(SleepMs):
    pass

# Low priority
class AfterMs(SleepMs):
    pass

class After(AfterMs):
    pass

after_ms = AfterMs()
after = After()

# High Priority
class When(SleepMs):
    pass

when = When()

def coroutine(f):
    return f

#
# The functions below are deprecated in uasyncio, and provided only
# for compatibility with CPython asyncio
#

def ensure_future(coro, loop=_event_loop):
    _event_loop.call_soon(coro)
    # CPython asyncio incompatibility: we don't return Task object
    return coro


# CPython asyncio incompatibility: Task is a function, not a class (for efficiency)
def Task(coro, loop=_event_loop):
    # Same as async()
    _event_loop.call_soon(coro)