# must be built with scons
from .messaging_pyx import Context, Poller, SubSocket, PubSocket, SocketEventHandle, toggle_fake_events, \
                                set_fake_prefix, get_fake_prefix, delete_fake_prefix, wait_for_one_event
from .messaging_pyx import MultiplePublishersError, MessagingError

import os
import capnp
import time

from typing import Optional, List, Union, Dict, Deque
from collections import deque

from cereal import log
from cereal.services import SERVICE_LIST

assert MultiplePublishersError
assert MessagingError
assert toggle_fake_events
assert set_fake_prefix
assert get_fake_prefix
assert delete_fake_prefix
assert wait_for_one_event

NO_TRAVERSAL_LIMIT = 2**64-1
AVG_FREQ_HISTORY = 100

context = Context()


def fake_event_handle(endpoint: str, identifier: Optional[str] = None, override: bool = True, enable: bool = False) -> SocketEventHandle:
  identifier = identifier or get_fake_prefix()
  handle = SocketEventHandle(endpoint, identifier, override)
  if override:
    handle.enabled = enable

  return handle


def log_from_bytes(dat: bytes) -> capnp.lib.capnp._DynamicStructReader:
  with log.Event.from_bytes(dat, traversal_limit_in_words=NO_TRAVERSAL_LIMIT) as msg:
    return msg


def new_message(service: Optional[str], size: Optional[int] = None, **kwargs) -> capnp.lib.capnp._DynamicStructBuilder:
  args = {
    'valid': False,
    'logMonoTime': int(time.monotonic() * 1e9),
    **kwargs
  }
  dat = log.Event.new_message(**args)
  if service is not None:
    if size is None:
      dat.init(service)
    else:
      dat.init(service, size)
  return dat


def pub_sock(endpoint: str) -> PubSocket:
  sock = PubSocket()
  sock.connect(context, endpoint)
  return sock


def sub_sock(endpoint: str, poller: Optional[Poller] = None, addr: str = "127.0.0.1",
             conflate: bool = False, timeout: Optional[int] = None) -> SubSocket:
  sock = SubSocket()
  sock.connect(context, endpoint, addr.encode('utf8'), conflate)

  if timeout is not None:
    sock.setTimeout(timeout)

  if poller is not None:
    poller.registerSocket(sock)
  return sock


def drain_sock_raw(sock: SubSocket, wait_for_one: bool = False) -> List[bytes]:
  """Receive all message currently available on the queue"""
  ret: List[bytes] = []
  while 1:
    if wait_for_one and len(ret) == 0:
      dat = sock.receive()
    else:
      dat = sock.receive(non_blocking=True)

    if dat is None:
      break

    ret.append(dat)

  return ret


def drain_sock(sock: SubSocket, wait_for_one: bool = False) -> List[capnp.lib.capnp._DynamicStructReader]:
  """Receive all message currently available on the queue"""
  ret: List[capnp.lib.capnp._DynamicStructReader] = []
  while 1:
    if wait_for_one and len(ret) == 0:
      dat = sock.receive()
    else:
      dat = sock.receive(non_blocking=True)

    if dat is None:  # Timeout hit
      break

    dat = log_from_bytes(dat)
    ret.append(dat)

  return ret


# TODO: print when we drop packets?
def recv_sock(sock: SubSocket, wait: bool = False) -> Optional[capnp.lib.capnp._DynamicStructReader]:
  """Same as drain sock, but only returns latest message. Consider using conflate instead."""
  dat = None

  while 1:
    if wait and dat is None:
      recv = sock.receive()
    else:
      recv = sock.receive(non_blocking=True)

    if recv is None:  # Timeout hit
      break

    dat = recv

  if dat is not None:
    dat = log_from_bytes(dat)

  return dat


def recv_one(sock: SubSocket) -> Optional[capnp.lib.capnp._DynamicStructReader]:
  dat = sock.receive()
  if dat is not None:
    dat = log_from_bytes(dat)
  return dat


def recv_one_or_none(sock: SubSocket) -> Optional[capnp.lib.capnp._DynamicStructReader]:
  dat = sock.receive(non_blocking=True)
  if dat is not None:
    dat = log_from_bytes(dat)
  return dat


def recv_one_retry(sock: SubSocket) -> capnp.lib.capnp._DynamicStructReader:
  """Keep receiving until we get a message"""
  while True:
    dat = sock.receive()
    if dat is not None:
      return log_from_bytes(dat)


class SubMaster:
  def __init__(self, services: List[str], poll: Optional[List[str]] = None,
               ignore_alive: Optional[List[str]] = None, ignore_avg_freq: Optional[List[str]] = None,
               ignore_valid: Optional[List[str]] = None, addr: str = "127.0.0.1"):
    self.frame = -1
    self.updated = {s: False for s in services}
    self.recv_time = {s: 0. for s in services}
    self.recv_frame = {s: 0 for s in services}
    self.alive = {s: False for s in services}
    self.freq_ok = {s: False for s in services}
    self.recv_dts: Dict[str, Deque[float]] = {s: deque(maxlen=AVG_FREQ_HISTORY) for s in services}
    self.sock = {}
    self.data = {}
    self.valid = {}
    self.logMonoTime = {}

    self.poller = Poller()
    self.non_polled_services = [s for s in services if poll is not None and
                                len(poll) and s not in poll]

    self.ignore_average_freq = [] if ignore_avg_freq is None else ignore_avg_freq
    self.ignore_alive = [] if ignore_alive is None else ignore_alive
    self.ignore_valid = [] if ignore_valid is None else ignore_valid
    if bool(int(os.getenv("SIMULATION", "0"))):
      self.ignore_alive = services
      self.ignore_average_freq = services

    for s in services:
      p = self.poller if s not in self.non_polled_services else None
      self.sock[s] = sub_sock(s, poller=p, addr=addr, conflate=True)

      try:
        data = new_message(s)
      except capnp.lib.capnp.KjException:
        data = new_message(s, 0) # lists

      self.data[s] = getattr(data.as_reader(), s)
      self.logMonoTime[s] = 0
      # TODO: this should default to False
      self.valid[s] = True

  def __getitem__(self, s: str) -> capnp.lib.capnp._DynamicStructReader:
    return self.data[s]

  def _check_avg_freq(self, s: str) -> bool:
    return self.recv_time[s] > 1e-5 and SERVICE_LIST[s].frequency > 1e-5 and (s not in self.non_polled_services) \
            and (s not in self.ignore_average_freq) and (s not in self.ignore_alive)

  def update(self, timeout: int = 1000) -> None:
    msgs = []
    for sock in self.poller.poll(timeout):
      msgs.append(recv_one_or_none(sock))

    # non-blocking receive for non-polled sockets
    for s in self.non_polled_services:
      msgs.append(recv_one_or_none(self.sock[s]))
    self.update_msgs(time.monotonic(), msgs)

  def update_msgs(self, cur_time: float, msgs: List[capnp.lib.capnp._DynamicStructReader]) -> None:
    self.frame += 1
    self.updated = dict.fromkeys(self.updated, False)
    for msg in msgs:
      if msg is None:
        continue

      s = msg.which()
      self.updated[s] = True

      self.recv_dts[s].append(cur_time - self.recv_time[s])
      self.recv_time[s] = cur_time
      self.recv_frame[s] = self.frame
      self.data[s] = getattr(msg, s)
      self.logMonoTime[s] = msg.logMonoTime
      self.valid[s] = msg.valid

    for s in self.data:
      if SERVICE_LIST[s].frequency > 1e-5:
        # alive if delay is within 10x the expected frequency
        self.alive[s] = (cur_time - self.recv_time[s]) < (10. / SERVICE_LIST[s].frequency)

        # TODO: check if update frequency is high enough to not drop messages
        # freq_ok if average frequency is higher than 90% of expected frequency
        if self._check_avg_freq(s):
          if len(self.recv_dts[s]) > 0:
            avg_dt = sum(self.recv_dts[s]) / len(self.recv_dts[s])
            expected_dt = 1 / (SERVICE_LIST[s].frequency * 0.90)
            self.freq_ok[s] = (avg_dt < expected_dt)
          else:
            self.freq_ok[s] = False
        else:
          self.freq_ok[s] = True
      else:
        self.freq_ok[s] = True
        self.alive[s] = True

  def all_alive(self, service_list: Optional[List[str]] = None) -> bool:
    if service_list is None:  # check all
      service_list = list(self.alive.keys())
    return all(self.alive[s] for s in service_list if s not in self.ignore_alive)

  def all_freq_ok(self, service_list: Optional[List[str]] = None) -> bool:
    if service_list is None:  # check all
      service_list = list(self.alive.keys())
    return all(self.freq_ok[s] for s in service_list if self._check_avg_freq(s))

  def all_valid(self, service_list: Optional[List[str]] = None) -> bool:
    if service_list is None:  # check all
      service_list = list(self.alive.keys())
    return all(self.valid[s] for s in service_list if s not in self.ignore_valid)

  def all_checks(self, service_list: Optional[List[str]] = None) -> bool:
    return self.all_alive(service_list=service_list) \
           and self.all_freq_ok(service_list=service_list) \
           and self.all_valid(service_list=service_list)


class PubMaster:
  def __init__(self, services: List[str]):
    self.sock = {}
    for s in services:
      self.sock[s] = pub_sock(s)

  def send(self, s: str, dat: Union[bytes, capnp.lib.capnp._DynamicStructBuilder]) -> None:
    if not isinstance(dat, bytes):
      dat = dat.to_bytes()
    self.sock[s].send(dat)

  def wait_for_readers_to_update(self, s: str, timeout: int, dt: float = 0.05) -> bool:
    for _ in range(int(timeout*(1./dt))):
      if self.sock[s].all_readers_updated():
        return True
      time.sleep(dt)
    return False

  def all_readers_updated(self, s: str) -> bool:
    return self.sock[s].all_readers_updated()  # type: ignore
