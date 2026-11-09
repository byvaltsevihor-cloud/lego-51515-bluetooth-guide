#!/usr/bin/env python3
"""Керування хабом LEGO MINDSTORMS Robot Inventor (51515) з Mac через Bluetooth, без кабелю.

На цьому macOS порт /dev/cu.LEGOHub... не передає дані, тому інструмент працює
напряму з каналом RFCOMM через системний фреймворк IOBluetooth і відкриває
MicroPython-консоль хаба.

Приклади:
    ./hub status                 стан хаба, портів і батареї
    ./hub exec "print(1 + 1)"    виконати Python-код на хабі
    ./hub run program.py         запустити локальний файл на хабі
    ./hub repl                   інтерактивна консоль хаба (вихід: Ctrl-])
    ./hub hello                  показати усмішку на екрані й подати сигнал
    ./hub motor A 90 25          повернути мотор A на 90 градусів зі швидкістю 25%
    ./hub stop                   зупинити всі мотори
    ./hub watch --seconds 20     показувати сенсори, кнопки й нахил наживо
    ./hub motortest              по черзі повернути кожен мотор на 30 градусів і назад
    ./hub backup                 скопіювати програми з хаба на Mac
    ./hub drive                  керувати Blast із клавіатури
    ./hub inforeply              перевірити відповідь хаба на запит застосунку про версію
    ./hub pad                    керувати Blast із контролера PlayStation 5 (--check: лише контролер)
"""

import argparse
import ast
import base64
import json
import math
import os
import re
import select
import subprocess
import sys
import tempfile
import termios
import time
import tty

import IOBluetooth
from Foundation import NSDate, NSObject, NSRunLoop

CTRL_A = b"\x01"
CTRL_B = b"\x02"
CTRL_C = b"\x03"
CTRL_D = b"\x04"
REPL_EXIT_KEY = b"\x1d"  # Ctrl-]
PROMPT = b">>> "
RAW_BANNER = b"raw REPL; CTRL-B to exit\r\n>"

SPP_UUID_PREFIX = "00001101"
RFCOMM_UUID_PREFIX = "00000003"
DEFAULT_SPP_CHANNEL = 1
CHUNK_SIZE = 128

TELEMETRY_MARK = b'{"m":'
RUNTIME_SETTLE_SECONDS = 6
MIN_SESSION_GAP_SECONDS = 12
LAST_SESSION_FILE = os.path.join(tempfile.gettempdir(), "lego-hub-last-session")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
BACKUP_DIR = os.path.join(PROJECT_DIR, "backup")
MAX_SESSION_LOGS = 50
BACKUP_BLOCK_SIZE = 180
BACKUP_BATCH_SIZE = 100
BACKUP_ROUNDS = 4


class HubError(Exception):
    """Помилка зв'язку з хабом, зрозуміла користувачеві."""


def iokit_code(result):
    return "0x%08x" % (result & 0xFFFFFFFF)


def wait_for_session_gap():
    """Витримує паузу після попереднього сеансу.

    Обидва вимкнення хаба сталися за секунду після швидкого повторного підключення.
    """
    try:
        elapsed = time.time() - os.path.getmtime(LAST_SESSION_FILE)
    except OSError:
        return
    if 0 <= elapsed < MIN_SESSION_GAP_SECONDS:
        time.sleep(MIN_SESSION_GAP_SECONDS - elapsed)


def stamp_session():
    with open(LAST_SESSION_FILE, "w"):
        pass


class SessionLog:
    """Сирий журнал сеансу: після збою в ньому видно, які байти пішли на хаб і прийшли від нього."""

    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        names = sorted(n for n in os.listdir(LOG_DIR) if n.startswith("session-"))
        for name in names[:-MAX_SESSION_LOGS]:
            os.remove(os.path.join(LOG_DIR, name))
        path = os.path.join(LOG_DIR, time.strftime("session-%Y%m%d-%H%M%S.log"))
        self.file = open(path, "a", buffering=1, encoding="utf-8")

    def write(self, kind, payload=""):
        self.file.write("%.3f %s %r\n" % (time.time(), kind, payload))


class _Delegate(NSObject):
    """Отримує дані й події закриття каналу від IOBluetooth."""

    def rfcommChannelData_data_length_(self, channel, data, length):
        self.link.on_data(bytes(data)[:length])

    def rfcommChannelClosed_(self, channel):
        self.link.on_closed()


def find_hub_address():
    """Шукає серед спарених пристроїв хаб за назвою."""
    env_address = os.environ.get("LEGO_HUB_ADDRESS")
    if env_address:
        return env_address
    paired = IOBluetooth.IOBluetoothDevice.pairedDevices() or []
    hubs = [d for d in paired if (d.name() or "").startswith("LEGO Hub")]
    if not hubs:
        raise HubError(
            "Mac не має спареного хаба LEGO. Увімкніть хаб, натисніть на ньому "
            "кнопку Bluetooth і додайте його в Налаштуваннях → Bluetooth."
        )
    if len(hubs) > 1:
        names = ", ".join(d.addressString() for d in hubs)
        raise HubError("Знайдено кілька хабів: %s. Вкажіть потрібний через --address." % names)
    return hubs[0].addressString()


def _walk_for_rfcomm(element):
    """Шукає номер каналу RFCOMM у SDP-записі ProtocolDescriptorList."""
    value = element.getValue()
    if element.getTypeDescriptor() not in (6, 7):
        return None
    items = list(value)
    if items and items[0].getTypeDescriptor() == 3:
        if bytes(items[0].getValue()).hex().startswith(RFCOMM_UUID_PREFIX) and len(items) > 1:
            return int(items[1].getValue())
    for item in items:
        found = _walk_for_rfcomm(item)
        if found is not None:
            return found
    return None


def spp_channel(device):
    """Повертає канал послуги Serial Port із кешованого SDP-запису."""
    for record in device.services() or []:
        service_class = record.getAttributeDataElement_(0x0001)
        descriptor = record.getAttributeDataElement_(0x0004)
        if service_class is None or descriptor is None:
            continue
        value = service_class.getValue()
        uuids = list(value) if service_class.getTypeDescriptor() in (6, 7) else [service_class]
        for uuid in uuids:
            raw = uuid.getValue() if hasattr(uuid, "getValue") else uuid
            if bytes(raw).hex().startswith(SPP_UUID_PREFIX):
                channel = _walk_for_rfcomm(descriptor)
                if channel is not None:
                    return channel
    return DEFAULT_SPP_CHANNEL


class HubLink:
    """З'єднання RFCOMM з MicroPython-консоллю хаба."""

    def __init__(self, address=None):
        self.address = address or find_hub_address()
        self.device = None
        self.channel = None
        self.rx = bytearray()
        self.closed = False
        self.raw_mode = False
        self.log = SessionLog()
        self.delegate = _Delegate.alloc().init()
        self.delegate.link = self

    def pump(self, seconds=0.02):
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))

    def on_data(self, data):
        self.rx.extend(data)
        self.log.write("RX", data)

    def on_closed(self):
        self.closed = True
        self.log.write("CHANNEL_CLOSED")

    def connect(self):
        wait_for_session_gap()
        # Позначка вже тут: невдале відкриття чи вбитий процес не скасують наступну паузу.
        stamp_session()
        self.device = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(self.address)
        if self.device is None:
            raise HubError("Некоректна адреса Bluetooth: %s" % self.address)
        result = self.device.openConnection()
        self.log.write("ACL", iokit_code(result))
        if result != 0:
            raise HubError(
                "Хаб не відповідає (код %s). Перевірте, що він увімкнений."
                % iokit_code(result)
            )
        status, channel = self.device.openRFCOMMChannelSync_withChannelID_delegate_(
            None, spp_channel(self.device), self.delegate
        )
        self.log.write("RFCOMM", iokit_code(status))
        if status != 0 or channel is None:
            raise HubError("Не вдалося відкрити канал RFCOMM (код %s)." % iokit_code(status))
        self.channel = channel
        self.pump(0.3)
        if self.closed or not channel.isOpen():
            raise HubError("Канал закрився одразу після відкриття.")

    def write(self, data):
        for start in range(0, len(data), CHUNK_SIZE):
            chunk = data[start : start + CHUNK_SIZE]
            result = self.channel.writeSync_length_(chunk, len(chunk))
            self.log.write("TX " + iokit_code(result), chunk)
            if result != 0:
                raise HubError("Помилка запису в канал (код %s)." % iokit_code(result))
            self.pump(0.02)

    def read_until(self, token, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            index = self.rx.find(token)
            if index >= 0:
                end = index + len(token)
                head = bytes(self.rx[:end])
                del self.rx[:end]
                return head
            if self.closed:
                raise HubError("Хаб закрив з'єднання. Він міг вимкнутися: перевірте живлення.")
            self.pump()
        raise HubError("Хаб не відповів за %.0f с. Отримано: %r" % (timeout, bytes(self.rx[-200:])))

    def interrupt(self):
        """Зупиняє runtime або програму на хабі й доводить його до тихої консолі.

        Робочий runtime перериваємо одним Ctrl-C і лише після паузи: пакет
        Ctrl-C Ctrl-C Ctrl-B одразу після підключення двічі збігся з вимкненням хаба.
        """
        self.pump(1.5)
        if TELEMETRY_MARK in self.rx:
            self.pump(RUNTIME_SETTLE_SECONDS)
            self.rx.clear()
            self.write(CTRL_C)
            self.read_until(PROMPT, 5)
            self.pump(0.3)
        for attempt in range(6):
            self.rx.clear()
            # Ctrl-B виводить із raw REPL; Ctrl-C потрібен, лише якщо щось досі виконується.
            self.write(CTRL_C if attempt in (2, 4) else CTRL_B)
            self.pump(0.8)
            if bytes(self.rx).rstrip(b"\r\n").endswith(PROMPT):
                self.rx.clear()
                self.pump(0.4)
                if not self.rx:
                    return
        raise HubError("Не вдалося отримати консоль хаба.")

    def enter_raw(self):
        self.interrupt()
        self.rx.clear()
        self.write(CTRL_A)
        self.read_until(RAW_BANNER, 5)
        self.raw_mode = True

    def leave_raw(self):
        if self.raw_mode:
            self.rx.clear()
            self.write(CTRL_B)
            self.read_until(PROMPT, 3)
            self.raw_mode = False

    def exec(self, code, timeout=60, stream=None):
        """Виконує код у raw REPL. Повертає (stdout, stderr)."""
        if not self.raw_mode:
            self.enter_raw()
        if isinstance(code, str):
            code = code.encode()
        self.rx.clear()
        self.write(code)
        self.write(CTRL_D)
        self.read_until(b"OK", 5)

        stdout = bytearray()
        deadline = time.time() + timeout
        while True:
            index = self.rx.find(CTRL_D)
            if index >= 0:
                chunk = bytes(self.rx[:index])
                del self.rx[: index + 1]
                stdout.extend(chunk)
                if stream and chunk:
                    stream(chunk)
                break
            if self.rx:
                chunk = bytes(self.rx)
                self.rx.clear()
                stdout.extend(chunk)
                if stream:
                    stream(chunk)
            if self.closed:
                raise HubError("Хаб закрив з'єднання під час виконання коду.")
            if time.time() > deadline:
                raise HubError("Код на хабі не завершився за %d с." % timeout)
            self.pump()
        stderr = self.read_until(CTRL_D, 5)[:-1]
        self.read_until(b">", 5)
        return bytes(stdout), bytes(stderr)

    def close(self, restore_runtime=True):
        """Закриває канал і радіозв'язок. Soft reboot повертає стандартну роботу хаба."""
        if self.channel is not None:
            try:
                if not self.closed:
                    self.leave_raw()
                    if restore_runtime:
                        self.write(CTRL_D)
                        self.pump(0.5)
            except HubError:
                pass
            self.channel.closeChannel()
            self.pump(0.3)
            self.channel = None
        if self.device is not None:
            self.device.closeConnection()
            self.device = None
        stamp_session()
        self.log.write("END")


STATUS_CODE = r"""
import hub, os, gc
def safe(f):
    try:
        return f()
    except Exception as e:
        return 'ERR %r' % (e,)
NAMES = {48: 'мотор середній (SPIKE)', 49: 'мотор великий (SPIKE)', 61: 'датчик кольору',
         62: 'датчик відстані', 63: 'датчик сили', 75: 'мотор середній (Inventor)',
         76: 'мотор великий (Inventor)'}
print('Прошивка:', os.uname().version)
print('Runtime:', safe(lambda: open('version.py').read().strip().replace('\n', ' ')[:100]))
print('Хаб:', hub.info().get('hardware_version'), '| варіант:', hub.info().get('product_variant'))
print('Батарея: %d%%, %.2f В, %d мА, %s °C, зарядка: %s' % (hub.battery.capacity_left(),
      hub.battery.voltage() / 1000, safe(hub.battery.current), safe(hub.battery.temperature),
      safe(hub.battery.charger_detect)))
for name in 'ABCDEF':
    port = getattr(hub.port, name)
    info = safe(port.info)
    kind = info.get('type') if isinstance(info, dict) else None
    device = port.device
    value = safe(device.get) if device else None
    print('Порт %s:' % name, NAMES.get(kind, kind) if kind else 'порожньо', '|', 'значення', value)
print('Положення (yaw, pitch, roll):', safe(hub.motion.position) if hasattr(hub.motion, 'position') else safe(hub.motion.yaw_pitch_roll))
fs = os.statvfs('/')
gc.collect()
print('Пам\'ять: вільно %d КБ RAM, %d КБ на диску із %d КБ' % (gc.mem_free() // 1024,
      fs[0] * fs[3] // 1024, fs[0] * fs[2] // 1024))
print('Збережені програми:', safe(lambda: len(eval(open('projects/.slots').read()))))
"""

HELLO_CODE = """
import hub, time
hub.display.show(hub.Image.HAPPY)
hub.sound.beep(523, 150)
time.sleep_ms(180)
hub.sound.beep(784, 250)
time.sleep_ms(1500)
print('hello ok')
"""

STOP_CODE = """
import hub
for name in 'ABCDEF':
    port = getattr(hub.port, name)
    if port.motor:
        port.motor.float()
print('motors stopped')
"""

MOTOR_CODE = """
import hub, time
motor = hub.port.{port}.motor
if motor is None:
    raise OSError('На порту {port} немає мотора')
start = motor.get()[1]
# Напрямок задає знак швидкості: знак градусів run_for_degrees ігнорує.
motor.run_for_degrees(abs({degrees}), abs({speed}) if {degrees} >= 0 else -abs({speed}))
time.sleep_ms(100)
deadline = time.ticks_add(time.ticks_ms(), 8000)
while motor.busy(motor.BUSY_MOTOR) and time.ticks_diff(deadline, time.ticks_ms()) > 0:
    time.sleep_ms(20)
time.sleep_ms(100)
print('Мотор {port}: було', start, 'стало', motor.get()[1])
motor.float()
"""

WATCH_CODE = """
import hub, time
MOTORS = (48, 49, 65, 75, 76)
def snapshot():
    parts = []
    for name in 'ABCDEF':
        port = getattr(hub.port, name)
        device = port.device
        if device is None:
            parts.append(name + '=-')
        elif port.info().get('type') in MOTORS:
            parts.append(name + '=' + str(port.motor.get()[2]))
        else:
            parts.append(name + '=' + str(device.get()))
    buttons = [hub.button.left.is_pressed(), hub.button.center.is_pressed(), hub.button.right.is_pressed()]
    parts.append('кнопки(Л,Ц,П)=' + ''.join('1' if b else '0' for b in buttons))
    parts.append('нахил=' + str(hub.motion.yaw_pitch_roll()))
    return '  '.join(parts)
hub.sound.beep(660, 150)
end = time.ticks_add(time.ticks_ms(), {seconds} * 1000)
last = None
while time.ticks_diff(end, time.ticks_ms()) > 0:
    line = snapshot()
    if line != last:
        print(line)
        last = line
    time.sleep_ms(200)
hub.sound.beep(440, 300)
"""


MOTORTEST_CODE = """
import hub, time
MOTORS = (48, 49, 65, 75, 76)
def turn(motor, degrees, speed):
    # Напрямок задає знак швидкості: знак градусів run_for_degrees ігнорує.
    start = motor.get()[1]
    motor.run_for_degrees(abs(degrees), speed if degrees >= 0 else -speed)
    time.sleep_ms(100)
    deadline = time.ticks_add(time.ticks_ms(), 4000)
    while motor.busy(motor.BUSY_MOTOR) and time.ticks_diff(deadline, time.ticks_ms()) > 0:
        time.sleep_ms(20)
    time.sleep_ms(150)
    return motor.get()[1] - start
for name in 'ABCDEF':
    port = getattr(hub.port, name)
    if port.device is None or port.info().get('type') not in MOTORS:
        continue
    motor = port.motor
    try:
        forward = turn(motor, {degrees}, {speed})
        back = turn(motor, -{degrees}, {speed})
    finally:
        motor.float()
    good = abs(forward - {degrees}) <= {tolerance} and abs(back + {degrees}) <= {tolerance}
    print('Мотор', name, '| вперед', forward, '| назад', back, '|', 'OK' if good else 'ПЕРЕВІРИТИ')
    time.sleep_ms(400)
print('motortest done')
"""


BACKUP_HELPERS = """
import os, ubinascii
def walk(path):
    for name in os.listdir(path):
        full = path + '/' + name
        if os.stat(full)[0] & 0x4000:
            for item in walk(full):
                yield item
        else:
            yield [full, os.stat(full)[6]]
def send(path, indexes):
    with open(path, 'rb') as source:
        for index in indexes:
            source.seek(index * {block_size})
            block = source.read({block_size})
            print(index, len(block), sum(block), ubinascii.b2a_base64(block).decode().strip())
""".format(block_size=BACKUP_BLOCK_SIZE)


def run_quiet(link, code, timeout=60):
    stdout, stderr = link.exec(code, timeout=timeout)
    if stderr:
        raise HubError("Хаб повідомив про помилку: %s" % stderr.decode(errors="replace").strip())
    return stdout


def fetch_file(link, remote, size):
    """Читає файл блоками й перевіряє кожен: при довгому виводі окремі байти можуть губитися."""
    count = math.ceil(size / BACKUP_BLOCK_SIZE)
    blocks = {}
    for _ in range(BACKUP_ROUNDS):
        missing = [i for i in range(count) if i not in blocks]
        if not missing:
            break
        for start in range(0, len(missing), BACKUP_BATCH_SIZE):
            batch = missing[start : start + BACKUP_BATCH_SIZE]
            stdout = run_quiet(link, "send(%r, %r)" % (remote, batch), timeout=90)
            for line in stdout.decode(errors="replace").splitlines():
                parts = line.split()
                if len(parts) != 4:
                    continue
                try:
                    index, length, checksum = int(parts[0]), int(parts[1]), int(parts[2])
                    block = base64.b64decode(parts[3], validate=True)
                except ValueError:
                    continue
                if len(block) == length and sum(block) == checksum:
                    blocks[index] = block
    data = b"".join(blocks[i] for i in range(count) if i in blocks)
    if len(data) != size:
        raise HubError("Файл %s прочитано не повністю: %d із %d байтів." % (remote, len(data), size))
    return data


def backup_projects(link):
    """Копіює програми з хаба на Mac. На самому хабі нічого не змінює."""
    run_quiet(link, BACKUP_HELPERS)
    listing = run_quiet(link, "print(list(walk('projects')))")
    files = ast.literal_eval(listing.decode().strip())
    target = os.path.join(BACKUP_DIR, time.strftime("projects-%Y%m%d-%H%M%S"))
    total = 0
    for remote, size in files:
        data = fetch_file(link, remote, size)
        local = os.path.join(target, remote)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as output:
            output.write(data)
        total += size
    print("Скопійовано файлів: %d, %d КБ" % (len(files), total // 1024))
    print("Папка: %s" % target)


def _echo(chunk):
    sys.stdout.write(chunk.decode(errors="replace"))
    sys.stdout.flush()


def run_code(link, code, timeout=60):
    _, stderr = link.exec(code, timeout=timeout, stream=_echo)
    if stderr:
        sys.stderr.write(stderr.decode(errors="replace"))
        return 1
    return 0


def repl(link):
    """Ретранслює клавіатуру в консоль хаба, вихід за Ctrl-]."""
    link.write(b"\r\n")
    stdin_fd = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin_fd)
    print("Консоль хаба. Вихід: Ctrl-]\r")
    try:
        tty.setraw(stdin_fd)
        while not link.closed:
            link.pump(0.01)
            if link.rx:
                os.write(sys.stdout.fileno(), bytes(link.rx))
                link.rx.clear()
            if select.select([stdin_fd], [], [], 0)[0]:
                before, exit_key, _ = os.read(stdin_fd, 256).partition(REPL_EXIT_KEY)
                if before:
                    link.write(before)
                if exit_key:
                    link.pump(0.5)
                    break
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        print()


DRIVE_HELPERS = """
import hub
_A, _B, _C, _D = hub.port.A.motor, hub.port.B.motor, hub.port.C.motor, hub.port.D.motor
def _walk(left, right, ms):
    _C.run_for_time(ms, left)
    _A.run_for_time(ms, right)
def _run(motor, speed, ms):
    motor.run_for_time(ms, speed)
def _stop():
    for motor in (_A, _B, _C, _D):
        motor.float()
print('ready', None not in (_A, _B, _C, _D), hasattr(_A, 'run_for_time'))
"""

DRIVE_HELP = """Керування Blast. Вихід: q. Зупинка: пробіл.
  стрілки  ходьба (мотори A і C)
  w / s    руки (мотор D)
  x / z    стрільба або удар (мотор B)
  + / -    швидкість
Працюють обидві розкладки. Кожне натискання вмикає мотори на {ms} мс, тож утримуйте клавішу.
"""

DRIVE_MS = 500
SHORT_MS = 300
ARM_SPEED = 25
WEAPON_SPEED = 30
MIN_DRIVE_SPEED = 15
MAX_DRIVE_SPEED = 75
DRIVE_SPEED_STEP = 10
DRIVE_KEY = re.compile("\x1b\\[[A-D]|.", re.DOTALL)
# Клавіші в українській розкладці дають інші символи, тому зводимо їх до латиниці.
KEY_ALIASES = {"ц": "w", "і": "s", "ч": "x", "я": "z", "й": "q", "ы": "s"}


def drive_command(key, speed):
    """Повертає код для хаба. Лівий мотор (C) і правий (A) дзеркальні, тому вперед вони мають різні знаки."""
    commands = {
        "\x1b[A": "_walk(%d,%d,%d)" % (-speed, speed, DRIVE_MS),
        "\x1b[B": "_walk(%d,%d,%d)" % (speed, -speed, DRIVE_MS),
        "\x1b[D": "_walk(%d,%d,%d)" % (speed, speed, DRIVE_MS),
        "\x1b[C": "_walk(%d,%d,%d)" % (-speed, -speed, DRIVE_MS),
        "w": "_run(_D,%d,%d)" % (ARM_SPEED, SHORT_MS),
        "s": "_run(_D,%d,%d)" % (-ARM_SPEED, SHORT_MS),
        "x": "_run(_B,%d,%d)" % (WEAPON_SPEED, SHORT_MS),
        "z": "_run(_B,%d,%d)" % (-WEAPON_SPEED, SHORT_MS),
        " ": "_stop()",
    }
    return commands.get(key)


def drive_check(link):
    """Перевірка без руху: помічники на хабі й час відповіді."""
    print(run_quiet(link, DRIVE_HELPERS).decode().strip())
    samples = []
    for _ in range(10):
        start = time.time()
        run_quiet(link, "pass")
        samples.append(time.time() - start)
    print("Відповідь хаба: найкраща %.0f мс, найгірша %.0f мс" % (min(samples) * 1000, max(samples) * 1000))


def drive(link, speed):
    """Керування з клавіатури. Працює лише у справжньому терміналі."""
    if not sys.stdin.isatty():
        raise HubError("Команду drive треба запускати у вікні Терміналу.")
    if b"True True" not in run_quiet(link, DRIVE_HELPERS):
        raise HubError("Очікувалися мотори на портах A, B, C і D.")
    print(DRIVE_HELP.format(ms=DRIVE_MS))
    print("Швидкість: %d" % speed)
    stdin_fd = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin_fd)
    try:
        tty.setcbreak(stdin_fd)
        while not link.closed:
            link.pump(0.01)
            if not select.select([stdin_fd], [], [], 0)[0]:
                continue
            # Беремо лише останню клавішу, щоб команди не накопичувалися в черзі.
            text = os.read(stdin_fd, 256).decode(errors="ignore")
            keys = DRIVE_KEY.findall(text)
            if not keys:
                continue
            key = KEY_ALIASES.get(keys[-1], keys[-1])
            if key == "q":
                break
            if key in ("+", "=", "-"):
                change = -DRIVE_SPEED_STEP if key == "-" else DRIVE_SPEED_STEP
                speed = max(MIN_DRIVE_SPEED, min(MAX_DRIVE_SPEED, speed + change))
                print("Швидкість: %d" % speed)
                continue
            command = drive_command(key, speed)
            if command:
                run_quiet(link, command, timeout=5)
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        if not link.closed:
            try:
                run_quiet(link, "_stop()", timeout=5)
            except HubError:
                pass


def build_parser():
    parser = argparse.ArgumentParser(description="Керування хабом LEGO 51515 з Mac через Bluetooth.")
    parser.add_argument("--address", help="Адреса Bluetooth хаба, якщо їх кілька")
    parser.add_argument(
        "--keep-prompt",
        action="store_true",
        help="Не перезапускати стандартний runtime хаба після команди",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Стан хаба, портів і батареї")
    commands.add_parser("hello", help="Усмішка на екрані й звуковий сигнал")
    commands.add_parser("stop", help="Зупинити всі мотори")
    commands.add_parser("repl", help="Інтерактивна консоль хаба")
    commands.add_parser("backup", help="Скопіювати програми з хаба на Mac")
    commands.add_parser("inforeply", help="Перевірити відповідь хаба на запит застосунку про версію")
    drive_parser = commands.add_parser("drive", help="Керувати Blast із клавіатури")
    drive_parser.add_argument("--speed", type=int, default=35, help="Початкова швидкість, типово 35")
    drive_parser.add_argument("--check", action="store_true", help="Перевірка без руху")
    pad_parser = commands.add_parser("pad", help="Керувати Blast із контролера PlayStation 5")
    pad_parser.add_argument("--speed", type=int, default=30, help="Початкова швидкість, типово 30")
    pad_parser.add_argument("--check", action="store_true", help="Перевірити контролер без хаба")
    run_exec = commands.add_parser("exec", help="Виконати Python-код на хабі")
    run_exec.add_argument("code")
    run_exec.add_argument("--timeout", type=int, default=60)
    run_file = commands.add_parser("run", help="Запустити локальний файл на хабі")
    run_file.add_argument("file")
    run_file.add_argument("--timeout", type=int, default=600)
    motor = commands.add_parser("motor", help="Повернути мотор на задану кількість градусів")
    motor.add_argument("port", choices=list("ABCDEF"))
    motor.add_argument("degrees", type=int)
    motor.add_argument("speed", type=int, nargs="?", default=25, help="Швидкість від 1 до 100, типово 25")
    watch = commands.add_parser("watch", help="Показувати значення портів, кнопок і нахилу наживо")
    watch.add_argument("--seconds", type=int, default=30)
    motortest = commands.add_parser("motortest", help="По черзі повернути кожен мотор туди й назад")
    motortest.add_argument("--degrees", type=int, default=30)
    motortest.add_argument("--speed", type=int, default=15)
    return parser


def competing_apps():
    """Повертає запущені застосунки LEGO, які можуть перехопити канал хаба."""
    names = []
    for name in ("SPIKE Legacy", "SPIKE", "LEGO MINDSTORMS Inventor"):
        result = subprocess.run(
            ["pgrep", "-f", "/Applications/%s.app/" % name], stdout=subprocess.DEVNULL
        )
        if result.returncode == 0:
            names.append(name)
    return names


def ensure_no_competing_apps():
    competing = competing_apps()
    if competing:
        raise HubError(
            "Спершу повністю закрийте %s (Cmd-Q): хаб приймає лише одне з'єднання."
            % ", ".join(competing)
        )


def info_reply_check(link):
    """Надсилає запит, яким застосунок дізнається версію, і перевіряє відповідь."""
    link.pump(5)
    if TELEMETRY_MARK not in link.rx:
        print("Стандартний runtime хаба не працює. Вимкніть і ввімкніть хаб, потім повторіть.")
        return 1
    link.rx.clear()
    link.write(b'{"i":"chk1","m":"get_hub_info","p":{}}\r')
    link.pump(2)
    received = bytes(link.rx).decode(errors="replace")
    start = received.find('{"i":"chk1"')
    if start < 0:
        print("Хаб не відповів на запит.")
        link.interrupt()
        return 1
    line = received[start:].split("\r")[0]
    try:
        reply = json.loads(line)["r"]
    except ValueError:
        print("Відповідь пошкоджена: без кінця повідомлення або обрізана. Див. розділ 1 у TROUBLESHOOTING.md.")
        link.interrupt()
        return 1
    print("Відповідь правильна, довжина: %d символів." % len(line))
    print("Прошивка: %s | runtime: %s" % (reply["firmware"]["version"], reply["runtime"]["version"]))
    link.interrupt()
    return 0


def open_ready_link(address):
    """Робить одну спробу підключення й доводить хаб до тихої консолі.

    Повторних викликів немає: збій зазвичай означає, що хаб вимкнений, і про це
    варто одразу сказати людині.
    """
    ensure_no_competing_apps()
    link = HubLink(address)
    try:
        link.connect()
        link.interrupt()
    except HubError:
        link.close(restore_runtime=False)
        raise
    return link


def main():
    args = build_parser().parse_args()
    link = None
    try:
        if args.command == "pad" and args.check:
            import ps5pad
            return ps5pad.check()
        if args.command == "inforeply":
            ensure_no_competing_apps()
            link = HubLink(args.address)
            link.connect()
            return info_reply_check(link)
        link = open_ready_link(args.address)
        if args.command == "backup":
            backup_projects(link)
            return 0
        if args.command == "pad":
            import ps5pad
            try:
                ps5pad.drive(link, run_quiet, DRIVE_HELPERS, args.speed)
            except RuntimeError as error:
                raise HubError(str(error))
            return 0
        if args.command == "drive":
            if args.check:
                drive_check(link)
            else:
                drive(link, max(MIN_DRIVE_SPEED, min(MAX_DRIVE_SPEED, args.speed)))
            return 0
        if args.command == "watch":
            code = WATCH_CODE.format(seconds=args.seconds)
            return run_code(link, code, timeout=args.seconds + 15)
        if args.command == "motortest":
            code = MOTORTEST_CODE.format(degrees=args.degrees, speed=args.speed, tolerance=6)
            return run_code(link, code, timeout=90)
        if args.command == "status":
            return run_code(link, STATUS_CODE)
        if args.command == "hello":
            return run_code(link, HELLO_CODE)
        if args.command == "stop":
            return run_code(link, STOP_CODE)
        if args.command == "exec":
            return run_code(link, args.code, timeout=args.timeout)
        if args.command == "run":
            with open(args.file, encoding="utf-8") as source:
                return run_code(link, source.read(), timeout=args.timeout)
        if args.command == "motor":
            if not 1 <= abs(args.speed) <= 100:
                raise HubError("Швидкість має бути від 1 до 100.")
            code = MOTOR_CODE.format(port=args.port, degrees=args.degrees, speed=args.speed)
            return run_code(link, code)
        if args.command == "repl":
            repl(link)
            return 0
    except HubError as error:
        print("Помилка: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if link and link.channel is not None:
            try:
                link.write(CTRL_C)
                link.pump(0.3)
            except HubError:
                pass
        return 130
    finally:
        if link is not None:
            link.close(restore_runtime=not args.keep_prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
