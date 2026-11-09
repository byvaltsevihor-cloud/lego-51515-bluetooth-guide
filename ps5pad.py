"""Керування Blast із контролера PlayStation 5 (DualSense) через Mac.

Контролер підключається до Mac по Bluetooth, бо хаб LEGO його напряму не підтримує.
Скрипт читає контролер через системний фреймворк GameController і передає команди
на хаб тим самим каналом RFCOMM, що й інші команди ./hub.

Розкладка:
    лівий стік        ходьба: вперед, назад, повороти (мотори C і A)
    правий стік ↕     руки (мотор D)
    R1 / L1           мотор B (ймовірно, стрільба або удар) короткими імпульсами
    △ / ✕             швидкість більша / менша
    ○                 зупинка
    Options           вихід

Кожна команда вмикає мотори лише на короткий час, тож якщо зв'язок обірветься,
робот сам зупиниться.
"""

import time

import GameController as GC
from Foundation import NSDate, NSRunLoop

DEADZONE = 0.15
SEND_INTERVAL = 0.12
PULSE_MS = 350
ARM_SPEED = 25
WEAPON_SPEED = 30
WEAPON_MS = 300
SPEED_STEPS = (20, 30, 40, 55, 75)


def pump(seconds):
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def find_controller(wait_seconds=5):
    """Повертає перший контролер із розширеною розкладкою або None."""
    # Терміналь не є «активним» застосунком, тому без цього прапорця macOS не передає події.
    GC.GCController.setShouldMonitorBackgroundEvents_(True)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        for controller in GC.GCController.controllers() or []:
            if controller.extendedGamepad() is not None:
                return controller
        pump(0.2)
    return None


def axis(value):
    """Прибирає мертву зону біля центру стіка."""
    if abs(value) < DEADZONE:
        return 0.0
    sign = 1 if value > 0 else -1
    return sign * (abs(value) - DEADZONE) / (1 - DEADZONE)


def walk_speeds(forward, turn, speed):
    """Повертає швидкості лівого (C) і правого (A) моторів.

    Мотори дзеркальні: вперед C крутиться у від'ємний бік, A у додатний.
    Поворот праворуч крутить обидва у від'ємний бік, як у ./hub drive.
    """
    left = -forward * speed - turn * speed
    right = forward * speed - turn * speed
    limit = speed
    return (
        int(max(-limit, min(limit, left))),
        int(max(-limit, min(limit, right))),
    )


def read_state(gamepad):
    return {
        "forward": axis(gamepad.leftThumbstick().yAxis().value()),
        "turn": axis(gamepad.leftThumbstick().xAxis().value()),
        "arms": axis(gamepad.rightThumbstick().yAxis().value()),
        "r1": gamepad.rightShoulder().isPressed(),
        "l1": gamepad.leftShoulder().isPressed(),
        "faster": gamepad.buttonY().isPressed(),
        "slower": gamepad.buttonA().isPressed(),
        "stop": gamepad.buttonB().isPressed(),
        "exit": gamepad.buttonMenu().isPressed(),
    }


def describe(controller):
    name = controller.vendorName() or "без назви"
    category = controller.productCategory() if hasattr(controller, "productCategory") else ""
    return "%s (%s)" % (name, category)


def check(seconds=15):
    """Перевірка без хаба: чи бачить Mac контролер і що він передає."""
    controller = find_controller()
    if controller is None:
        print("Контролер не знайдено. Спаруйте його з Mac: утримуйте Create + PS, "
              "поки не заблимає підсвітка, і додайте в Налаштуваннях → Bluetooth.")
        return 1
    print("Знайдено: %s. Порухайте стіками %d секунд." % (describe(controller), seconds))
    gamepad = controller.extendedGamepad()
    last = None
    end = time.time() + seconds
    while time.time() < end:
        pump(0.1)
        state = read_state(gamepad)
        line = "вперед %+.2f  поворот %+.2f  руки %+.2f  R1 %d L1 %d  △ %d ✕ %d ○ %d" % (
            state["forward"], state["turn"], state["arms"], state["r1"], state["l1"],
            state["faster"], state["slower"], state["stop"])
        if line != last:
            print(line)
            last = line
    return 0


def drive(link, run_quiet, helpers, start_speed):
    """Керування роботом. run_quiet і helpers передає hubctl."""
    controller = find_controller()
    if controller is None:
        raise RuntimeError("Контролер не знайдено. Спершу виконайте ./hub pad --check.")
    if b"True True" not in run_quiet(link, helpers):
        raise RuntimeError("Очікувалися мотори на портах A, B, C і D.")
    gamepad = controller.extendedGamepad()
    speed_index = min(range(len(SPEED_STEPS)), key=lambda i: abs(SPEED_STEPS[i] - start_speed))
    print("Контролер: %s. Швидкість: %d. Вихід: Options." % (describe(controller), SPEED_STEPS[speed_index]))
    moving = False
    previous = {}
    try:
        while not link.closed:
            pump(SEND_INTERVAL)
            state = read_state(gamepad)
            pressed = {k: v for k, v in state.items() if isinstance(v, bool) and v and not previous.get(k)}
            previous = state
            if state["exit"]:
                break
            if pressed.get("faster") or pressed.get("slower"):
                step = 1 if pressed.get("faster") else -1
                speed_index = max(0, min(len(SPEED_STEPS) - 1, speed_index + step))
                print("Швидкість: %d" % SPEED_STEPS[speed_index])
            if state["stop"]:
                run_quiet(link, "_stop()", timeout=5)
                moving = False
                continue
            commands = []
            speed = SPEED_STEPS[speed_index]
            if state["forward"] or state["turn"]:
                left, right = walk_speeds(state["forward"], state["turn"], speed)
                commands.append("_walk(%d,%d,%d)" % (left, right, PULSE_MS))
            if state["arms"]:
                commands.append("_run(_D,%d,%d)" % (int(state["arms"] * ARM_SPEED) or 1, PULSE_MS))
            if state["r1"] or state["l1"]:
                direction = 1 if state["r1"] else -1
                commands.append("_run(_B,%d,%d)" % (direction * WEAPON_SPEED, WEAPON_MS))
            if commands:
                run_quiet(link, ";".join(commands), timeout=5)
                moving = True
            elif moving:
                run_quiet(link, "_stop()", timeout=5)
                moving = False
    finally:
        if not link.closed:
            try:
                run_quiet(link, "_stop()", timeout=5)
            except Exception:
                pass
