"""The owner of the hardware in another process: `clockwork serve`'s socket and its client.

`DaemonServer` puts an owner behind two loopback ZeroMQ sockets, a command socket that
answers one JSON request at a time and an event socket that publishes every numbered
event as it happens. `RemoteOwner` is the client, and implements the `Owner` protocol
over those sockets, so a front end written against the in-process `LocalOwner` runs
against a daemon unchanged. Every wire fact -- ports, envelope, commands, topics, the
replay rule -- is `docs/daemon-protocol.md`; this module implements that document and
decides nothing about the protocol itself (lab record, task 68).

**The command path never waits on hardware.** A `submit` queues and returns; progress is
read with `events`. So a request is answered in milliseconds whatever the owner is doing,
and the daemon's command loop, which also publishes, can be one thread.

**The stream is the fast path and `events` is the record.** PUB drops what it cannot
deliver and says nothing, so the client keeps its own copy of each job's progress from
the stream and trusts it only while it can prove it has missed nothing: every event of
every job is numbered by one counter and published in that order (`LocalOwner.listen`),
so a subscriber to everything sees consecutive numbers and a gap is a loss. The copy of a
job is filled by one `events` call the first time it is read and after any gap, and until
the first heartbeat arrives every read is a request.

Qt-free, like the rest of `clockwork.owner`. A ZeroMQ socket belongs to one thread: the
server's two belong to whichever thread runs `serve`, the client's command socket is
shared under a lock, and its subscriber lives on a thread of its own.
"""

from __future__ import annotations

import inspect
import itertools
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

import zmq

from .. import __version__
from ..acq import BatchSeen, Snapshot
from .interface import Handle, JobFinished, OwnerStatus, Progress, Said, StaleHandle
from .jobs import Acquire, Job, Send
from .local import sentence
from .wire import from_wire, to_wire

__all__ = [
    "COMMAND_PORT",
    "DEFAULT_COMMAND",
    "DEFAULT_EVENTS",
    "DEFAULT_TIMEOUT_S",
    "EVENTS_PORT",
    "HEARTBEAT_S",
    "PROTOCOL",
    "DaemonError",
    "DaemonRefused",
    "DaemonServer",
    "DaemonUnavailable",
    "Hello",
    "RemoteOwner",
    "serve",
]

PROTOCOL = 1
"""The protocol version `hello` reports and `RemoteOwner` requires."""

COMMAND_PORT = 5570
EVENTS_PORT = 5571
"""Clear of the console's 5554 and 5555 (`docs/daemon-protocol.md`, Sockets)."""

DEFAULT_COMMAND = f"tcp://127.0.0.1:{COMMAND_PORT}"
DEFAULT_EVENTS = f"tcp://127.0.0.1:{EVENTS_PORT}"

HEARTBEAT_S = 1.0
DEFAULT_TIMEOUT_S = 5.0
"""How long a client waits for a reply. Every command is answered in milliseconds, so
this is the time it takes to decide that no daemon is there, not room for work."""

_POLL_MS = 50
_HANDLE_TOPIC = "handle/{}/"
_HEARTBEAT_TOPIC = "heartbeat/"


class DaemonError(RuntimeError):
    """Something between a client and the daemon went wrong. The message is a sentence."""


class DaemonUnavailable(DaemonError):
    """No daemon answered in time. The client has reset its socket and can be used again."""


class DaemonRefused(DaemonError):
    """The daemon understood the request and declined it, or failed answering it.

    `kind` is the protocol's word for which (`bad-request`, `unknown-command`, `refused`,
    `failed`)."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class _Refusal(Exception):
    """Raised inside a command handler to send an error reply of a given kind."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# -- the server --------------------------------------------------------------------


class DaemonServer:
    """An owner behind the command and event sockets.

    The sockets are bound by the constructor, so a port that is taken is a
    `DaemonError` here, before anything else starts; `command_endpoint` and
    `events_endpoint` are the addresses actually bound, which is how a test that asked
    for port `*` learns which it got. `serve` runs the loop on the calling thread until a
    shutdown has been asked for and the owner's thread has ended. Built on one thread and
    served on another is fine, as long as only the serving thread touches it afterwards.

    `owner` is a `LocalOwner`, or anything with the protocol's six calls plus `listen`,
    `join` and `closing`. `info` is added to what `hello` answers (`output`, `library`).
    `defaults` fills the `directory` of a `Send` or an `Acquire` that names none.
    `on_request` is told, in one line, every request that changes something.
    """

    def __init__(
        self,
        owner: object,
        *,
        command: str = DEFAULT_COMMAND,
        events: str = DEFAULT_EVENTS,
        info: Mapping[str, object] | None = None,
        directory: str = "",
        on_request: Callable[[str], None] | None = None,
        context: zmq.Context | None = None,
    ) -> None:
        self.owner = owner
        self.session: str = getattr(owner, "session", "") or os.urandom(6).hex()
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._info = dict(info or {})
        self._directory = directory
        self._on_request = on_request
        self._context = context if context is not None else zmq.Context.instance()
        self._outbox: list[tuple[Handle, Progress]] = []
        self._outbox_guard = threading.Lock()
        self._published = 0
        """The number of the last event published, which the heartbeat carries."""
        self._shutdown_reason: str | None = None
        self._shutdown_asked = threading.Event()

        self._router = self._context.socket(zmq.ROUTER)
        self._pub = self._context.socket(zmq.PUB)
        for socket in (self._router, self._pub):
            socket.setsockopt(zmq.LINGER, 0)
        # A slow subscriber loses messages at the high-water mark and catches up over
        # `events`; the mark is set high so that this is a fault and not a habit.
        self._pub.setsockopt(zmq.SNDHWM, 100_000)
        try:
            self._router.bind(command)
            self._pub.bind(events)
        except zmq.ZMQError as exc:
            self._router.close(linger=0)
            self._pub.close(linger=0)
            raise DaemonError(
                f"could not listen on {command} and {events} ({exc}); another clockwork "
                "serve is most likely running already") from exc
        self.command_endpoint = _bound(self._router)
        self.events_endpoint = _bound(self._pub)
        owner.listen(self._enqueue)  # type: ignore[attr-defined]

    # -- the loop ----------------------------------------------------------------

    def serve(self) -> None:
        """Answer and publish until shut down and the owner has finished, then close."""
        heartbeat_due = 0.0
        poller = zmq.Poller()
        poller.register(self._router, zmq.POLLIN)
        try:
            while True:
                if self._shutdown_asked.is_set() and not self.owner.closing:  # type: ignore[attr-defined]
                    self.owner.shutdown(self._shutdown_reason or "shut down")  # type: ignore[attr-defined]
                self._publish_pending()
                now = time.monotonic()
                if now >= heartbeat_due:
                    self._heartbeat()
                    heartbeat_due = now + HEARTBEAT_S
                if self.owner.closing and self.owner.join(0):  # type: ignore[attr-defined]
                    self._publish_pending()
                    self._heartbeat()
                    return
                if dict(poller.poll(_POLL_MS)).get(self._router):
                    self._answer_waiting()
        finally:
            self._router.close(linger=0)
            self._pub.close(linger=0)

    def request_shutdown(self, reason: str = "shut down") -> None:
        """Ask for the shutdown from any thread; the loop carries it out."""
        if self._shutdown_reason is None:
            self._shutdown_reason = reason
        self._shutdown_asked.set()

    @property
    def shutting_down(self) -> bool:
        return self._shutdown_asked.is_set()

    # -- publishing --------------------------------------------------------------

    def _enqueue(self, handle: Handle, progress: Progress) -> None:
        """`LocalOwner.listen`'s callback: called under the owner's lock, so it only
        appends. The loop publishes in the order entries arrive here, which is the order
        they were numbered."""
        with self._outbox_guard:
            self._outbox.append((handle, progress))

    def _publish_pending(self) -> None:
        with self._outbox_guard:
            pending, self._outbox = self._outbox, []
        for handle, progress in pending:
            payload = json.dumps(_carried(progress), ensure_ascii=False,
                                 separators=(",", ":"))
            self._pub.send_multipart([_HANDLE_TOPIC.format(handle.id).encode("ascii"),
                                      payload.encode("utf-8")])
            self._published = progress.seq

    def _heartbeat(self) -> None:
        payload = json.dumps({"session": self.session, "seq": self._published,
                              "time": time.time()})
        self._pub.send_multipart([_HEARTBEAT_TOPIC.encode("ascii"),
                                  payload.encode("utf-8")])

    # -- answering ---------------------------------------------------------------

    def _answer_waiting(self) -> None:
        while True:
            try:
                frames = self._router.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                return
            # ROUTER prefixes the peer's identity; a DEALER or REQ peer then sends an
            # empty delimiter, which goes back in front of the reply.
            if len(frames) < 2:
                continue
            envelope, payload = frames[:-1], frames[-1]
            reply = self._answer(payload)
            self._router.send_multipart(
                [*envelope, json.dumps(reply, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8")])

    def _answer(self, payload: bytes) -> dict[str, object]:
        request_id: object = None
        try:
            try:
                request = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise _Refusal("bad-request", f"the request was not JSON ({exc})") from exc
            if not isinstance(request, dict) or not isinstance(request.get("cmd"), str):
                raise _Refusal("bad-request",
                               "a request is a JSON object with a string `cmd`")
            request_id = request.get("id")
            args = request.get("args") or {}
            if not isinstance(args, dict):
                raise _Refusal("bad-request", "`args` is a JSON object")
            handler = _COMMANDS.get(request["cmd"])
            if handler is None:
                raise _Refusal("unknown-command",
                               f"there is no command {request['cmd']!r}; the commands are "
                               + ", ".join(sorted(_COMMANDS)))
            try:
                decoded = {key: from_wire(value) for key, value in args.items()}
            except (TypeError, ValueError, KeyError) as exc:
                raise _Refusal("bad-request", f"an argument could not be decoded: {exc}") \
                    from exc
            try:
                inspect.signature(handler).bind(self, **decoded)
            except TypeError as exc:
                raise _Refusal("bad-request",
                               f"{request['cmd']!r} was sent the wrong arguments: {exc}") \
                    from exc
            result = handler(self, **decoded)
            return {"id": request_id, "ok": True, "result": result,
                    "session": self.session}
        except _Refusal as exc:
            return {"id": request_id, "ok": False,
                    "error": {"kind": exc.kind, "message": str(exc)},
                    "session": self.session}
        except Exception as exc:  # noqa: BLE001 -- one sentence back, never a traceback
            return {"id": request_id, "ok": False,
                    "error": {"kind": "failed", "message": sentence(exc)},
                    "session": self.session}

    def _said(self, line: str) -> None:
        if self._on_request is not None:
            self._on_request(line)

    # -- the commands ------------------------------------------------------------

    def _hello(self) -> dict[str, object]:
        status: OwnerStatus = self.owner.status()  # type: ignore[attr-defined]
        return {
            "program": status.program, "version": __version__, "protocol": PROTOCOL,
            "session": self.session, "pid": os.getpid(), "started": self.started,
            "fake": status.fake, "events": self.events_endpoint,
            "output": self._directory, **self._info,
            "holder": status.holder.text if status.holder is not None else None,
        }

    def _submit(self, job: object = None) -> object:
        if not isinstance(job, Job):
            raise _Refusal("refused", f"`submit` takes a job, not {type(job).__name__}")
        if self.shutting_down or self.owner.closing:  # type: ignore[attr-defined]
            raise _Refusal("refused", "clockwork serve is shutting down and takes no "
                                      "more jobs")
        if isinstance(job, (Send, Acquire)) and not job.directory and self._directory:
            job = replace(job, directory=self._directory)
        handle = self.owner.submit(job)  # type: ignore[attr-defined]
        self._said(f"submitted job {handle.id}: {job.label} ({handle.kind})")
        return to_wire(handle)

    def _events(self, handle: object = None, after: object = 0) -> object:
        if not isinstance(handle, Handle):
            raise _Refusal("bad-request", "`events` takes a handle")
        if not isinstance(after, int):
            raise _Refusal("bad-request", "`after` is a whole number")
        try:
            entries = self.owner.events(handle, after)  # type: ignore[attr-defined]
        except StaleHandle as exc:
            raise _Refusal("refused", str(exc)) from exc
        return [_carried(entry) for entry in entries]

    def _stop(self, reason: object = "stopped by a client") -> None:
        self._said(f"stop asked for: {reason}")
        self.owner.stop(str(reason))  # type: ignore[attr-defined]

    def _snapshot(self) -> object:
        return to_wire(self.owner.snapshot())  # type: ignore[attr-defined]

    def _status(self) -> object:
        return to_wire(self.owner.status())  # type: ignore[attr-defined]

    def _shutdown(self, reason: object = "shut down by a client") -> None:
        self._said(f"shutdown asked for: {reason}")
        self.request_shutdown(str(reason))


_COMMANDS: dict[str, Callable[..., object]] = {
    "hello": DaemonServer._hello,
    "submit": DaemonServer._submit,
    "events": DaemonServer._events,
    "stop": DaemonServer._stop,
    "snapshot": DaemonServer._snapshot,
    "status": DaemonServer._status,
    "shutdown": DaemonServer._shutdown,
}


def serve(owner: object, *, command: str = DEFAULT_COMMAND,
          events: str = DEFAULT_EVENTS, **options: object) -> None:
    """Put `owner` behind the two sockets and serve until it is shut down."""
    DaemonServer(owner, command=command, events=events, **options).serve()  # type: ignore[arg-type]


def _carried(progress: Progress) -> object:
    """A `Progress` as its wire form, or a `Said` of the same number where the event has
    none: a new kind of event must not be able to stall the stream or an `events` reply.

    **A `JobFinished` stays a `JobFinished`**, without its result, since the event that
    ends a job is the one a client is waiting for; as a `Said` it left every client
    waiting on a job the daemon had finished (lab record, task 73)."""
    try:
        return to_wire(progress)
    except TypeError as exc:
        event = progress.event
        if isinstance(event, JobFinished):
            try:
                return to_wire(Progress(seq=progress.seq,
                                        event=replace(event, result=None)))
            except TypeError:
                pass
        return to_wire(Progress(seq=progress.seq, event=Said(
            line=f"(a {type(progress.event).__name__} event could not cross to another "
                 f"process: {exc})")))


def _bound(socket: zmq.Socket) -> str:
    endpoint = socket.getsockopt(zmq.LAST_ENDPOINT)
    return endpoint.decode("ascii") if isinstance(endpoint, bytes) else str(endpoint)


# -- the client --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hello:
    """What `hello` answered: which daemon this is, and where its events are."""

    program: str
    version: str
    protocol: int
    session: str
    pid: int
    started: str
    fake: bool
    events: str
    output: str = ""
    library: str = ""
    holder: str | None = None


@dataclass
class _Copy:
    """The client's copy of one job's progress, off the stream."""

    entries: list[Progress]
    trusted: bool = False
    """Filled by an `events` call that the stream has carried on from without a gap."""


class RemoteOwner:
    """The `Owner` protocol, spoken to a `clockwork serve` over its sockets.

    Built without touching the network: a daemon that is not running yet looks exactly
    like one that is until the first request, which raises `DaemonUnavailable` after
    `timeout`. Any thread may call any method. `close()` (or the `with` block) ends the
    subscriber thread and both sockets; nothing here stops the daemon but `shutdown`.

    A restarted daemon is picked up by the next request, whatever it is: its session is
    new, so every copy of progress is thrown away and a handle from the old one is
    refused with `StaleHandle`.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_COMMAND,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        context: zmq.Context | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._context = context if context is not None else zmq.Context.instance()
        self._ids = itertools.count(1)
        self._request_guard = threading.Lock()
        self._socket: zmq.Socket | None = None
        self._hello: Hello | None = None
        self._session: str | None = None

        self._state = threading.Condition()
        """Over the copies and everything the subscriber knows; notified whenever the
        subscriber adds something, which is what `wait` sleeps on."""
        self._copies: dict[int, _Copy] = {}
        self._run_from: int | None = None
        """The first number of the unbroken run of events the subscriber has seen, or
        None until its first message. A copy filled by an `events` reply is trusted only
        if the reply's newest-known number falls inside this run."""
        self._last_seq = 0
        self._last_heard = 0.0
        self._subscriber: threading.Thread | None = None
        self._events_endpoint: str | None = None
        self._closed = threading.Event()

    # -- the protocol --------------------------------------------------------------

    def submit(self, job: Job) -> Handle:
        return self._request("submit", job=job)  # type: ignore[return-value]

    def events(self, handle: Handle, after: int = 0) -> list[Progress]:
        """That job's progress after `after`, from the stream's copy when it can be
        trusted and from the daemon otherwise."""
        self._ensure_connected()
        with self._state:
            copy = self._copies.get(handle.id)
            if copy is not None and copy.trusted and self._owns(handle):
                return [entry for entry in copy.entries if entry.seq > after]
            streaming = self._run_from is not None
            known = self._last_seq
        if not streaming:
            return list(self._fetch(handle, after))
        entries = self._fetch(handle, 0)
        with self._state:
            copy = self._copies.setdefault(handle.id, _Copy(entries=[]))
            newest = entries[-1].seq if entries else 0
            copy.entries = list(entries) + [entry for entry in copy.entries
                                            if entry.seq > newest]
            # Everything numbered after `known` reached the subscriber unbroken, and
            # the reply holds everything up to at least `known`, so the two together
            # miss nothing -- unless the run began after `known`.
            copy.trusted = (self._run_from is not None and self._run_from <= known + 1
                            and self._owns(handle))
            return [entry for entry in copy.entries if entry.seq > after]

    def stop(self, reason: str = "stopped by the operator") -> None:
        self._request("stop", reason=reason)

    def snapshot(self) -> Snapshot | None:
        return self._request("snapshot")  # type: ignore[return-value]

    def status(self) -> OwnerStatus:
        return self._request("status")  # type: ignore[return-value]

    def shutdown(self, reason: str = "shut down by a client") -> None:
        self._request("shutdown", reason=reason)

    # -- beside the protocol -------------------------------------------------------

    def hello(self) -> Hello:
        """Ask the daemon who it is. Also what every other call does first, once."""
        data = self._request("hello", _raw=True)
        if not isinstance(data, dict):
            raise DaemonError(f"`hello` answered {data!r}")
        if data.get("protocol") != PROTOCOL:
            raise DaemonError(
                f"the daemon at {self.endpoint} speaks protocol {data.get('protocol')!r} "
                f"and this client speaks {PROTOCOL}; update one of them")
        fields = {name: data[name] for name in Hello.__dataclass_fields__ if name in data}
        hello = Hello(**fields)
        self._hello = hello
        self._start_subscriber(hello.events)
        return hello

    @property
    def streaming(self) -> bool:
        """Whether the subscriber has heard the daemon in the last three heartbeats."""
        return time.monotonic() - self._last_heard < 3 * HEARTBEAT_S

    def wait(self, handle: Handle, after: int = 0,
             timeout: float = 1.0) -> list[Progress]:
        """`events`, but block up to `timeout` for something newer than `after` to
        arrive. For a follower that would otherwise poll."""
        deadline = time.monotonic() + timeout
        while True:
            found = self.events(handle, after)
            left = deadline - time.monotonic()
            if found or left <= 0:
                return found
            with self._state:
                self._state.wait(min(left, 0.25))

    def close(self) -> None:
        self._closed.set()
        if self._subscriber is not None:
            self._subscriber.join(timeout=2.0)
            self._subscriber = None
        with self._request_guard:
            if self._socket is not None:
                self._socket.close(linger=0)
                self._socket = None

    def __enter__(self) -> RemoteOwner:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- requests ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._hello is None:
            self.hello()

    def _owns(self, handle: Handle) -> bool:
        return not handle.owner or handle.owner == self._session

    def _fetch(self, handle: Handle, after: int) -> tuple[Progress, ...]:
        try:
            return self._request("events", handle=handle, after=after)  # type: ignore[return-value]
        except DaemonRefused as exc:
            if exc.kind == "refused" and self._session and handle.owner \
                    and handle.owner != self._session:
                raise StaleHandle(str(exc)) from None
            raise

    def _request(self, cmd: str, *, _raw: bool = False, **args: object) -> object:
        if cmd != "hello":
            self._ensure_connected()
        request_id = next(self._ids)
        payload = json.dumps({"id": request_id, "cmd": cmd, "args": to_wire(args)},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._request_guard:
            if self._closed.is_set():
                raise DaemonError("this RemoteOwner is closed")
            if self._socket is None:
                self._open_socket()
            assert self._socket is not None
            self._socket.send_multipart([b"", payload])
            deadline = time.monotonic() + self.timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or not self._socket.poll(int(left * 1000), zmq.POLLIN):
                    # Thrown away so that a reply arriving late cannot be read as the
                    # answer to the next request; ZeroMQ reconnects the new one itself.
                    self._socket.close(linger=0)
                    self._socket = None
                    raise DaemonUnavailable(
                        f"no clockwork serve answered {cmd!r} at {self.endpoint} within "
                        f"{self.timeout:g} s; start one with `clockwork serve`, or check "
                        "the one that is running")
                frames = self._socket.recv_multipart()
                reply = json.loads(frames[-1].decode("utf-8"))
                if isinstance(reply, dict) and reply.get("id") == request_id:
                    break
        self._note_session(reply.get("session"))
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise DaemonRefused(str(error.get("message", "the daemon refused")),
                                str(error.get("kind", "failed")))
        result = reply.get("result")
        return result if _raw else from_wire(result)

    def _open_socket(self) -> None:
        socket = self._context.socket(zmq.DEALER)
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(self.endpoint)
        self._socket = socket

    def _note_session(self, session: object) -> None:
        """A new session is a new daemon: every copy was of the old one's jobs."""
        if not isinstance(session, str) or session == self._session:
            return
        restarted = self._session is not None
        self._session = session
        with self._state:
            self._copies.clear()
            self._run_from = None
            self._last_seq = 0
        if restarted:
            # Its event socket may be somewhere else now; ask on the next call.
            self._hello = None

    # -- the subscriber ------------------------------------------------------------

    def _start_subscriber(self, endpoint: str) -> None:
        if self._subscriber is not None and self._events_endpoint == endpoint:
            return
        if self._subscriber is not None:
            self._events_endpoint = endpoint  # the running thread reconnects
            return
        self._events_endpoint = endpoint
        self._subscriber = threading.Thread(target=self._listen, name="clockwork events",
                                            daemon=True)
        self._subscriber.start()

    def _listen(self) -> None:
        connected: str | None = None
        socket: zmq.Socket | None = None
        try:
            while not self._closed.is_set():
                if connected != self._events_endpoint:
                    if socket is not None:
                        socket.close(linger=0)
                    socket = self._context.socket(zmq.SUB)
                    socket.setsockopt(zmq.LINGER, 0)
                    socket.setsockopt(zmq.RCVHWM, 100_000)
                    socket.setsockopt(zmq.SUBSCRIBE, b"")
                    connected = self._events_endpoint
                    socket.connect(connected)  # type: ignore[arg-type]
                    with self._state:
                        self._run_from = None
                        for copy in self._copies.values():
                            copy.trusted = False
                assert socket is not None
                if not socket.poll(100, zmq.POLLIN):
                    continue
                while True:
                    try:
                        topic, payload = socket.recv_multipart(zmq.NOBLOCK)[:2]
                    except zmq.Again:
                        break
                    except ValueError:
                        continue
                    self._heard(topic, payload)
        finally:
            if socket is not None:
                socket.close(linger=0)

    def _heard(self, topic: bytes, payload: bytes) -> None:
        self._last_heard = time.monotonic()
        data = json.loads(payload.decode("utf-8"))
        with self._state:
            if topic == _HEARTBEAT_TOPIC.encode("ascii"):
                if data.get("session") != self._session and self._session is not None:
                    return  # a new daemon; the next request notices and resets
                seq = int(data.get("seq", 0))
                if self._run_from is None:
                    self._run_from, self._last_seq = seq + 1, seq
                elif seq > self._last_seq:
                    self._broken(seq + 1)
                self._state.notify_all()
                return
            try:
                handle_id = int(topic.decode("ascii").split("/")[1])
                progress = from_wire(data)
            except (ValueError, IndexError, TypeError):
                return
            if not isinstance(progress, Progress):
                return
            if self._run_from is None:
                self._run_from = progress.seq
            elif progress.seq != self._last_seq + 1:
                self._broken(progress.seq)
            self._last_seq = max(self._last_seq, progress.seq)
            copy = self._copies.setdefault(handle_id, _Copy(entries=[]))
            if copy.entries and progress.seq <= copy.entries[-1].seq:
                return
            if (isinstance(progress.event, BatchSeen) and copy.entries
                    and isinstance(copy.entries[-1].event, BatchSeen)):
                copy.entries[-1] = progress
            else:
                copy.entries.append(progress)
            self._state.notify_all()

    def _broken(self, resume: int) -> None:
        """Messages were lost: the run starts again here and no copy is trusted until
        it has been filled again. Under `_state`."""
        self._run_from = resume
        for copy in self._copies.values():
            copy.trusted = False
