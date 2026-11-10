import machine
import time
import sys
import select

RPWM = machine.PWM(machine.Pin(2))
LPWM = machine.PWM(machine.Pin(3))
REN  = machine.Pin(4, machine.Pin.OUT)
LEN  = machine.Pin(5, machine.Pin.OUT)

RPWM.freq(500)
LPWM.freq(500)

ENC_A = machine.Pin(27, machine.Pin.IN, machine.Pin.PULL_UP)
ENC_B = machine.Pin(28, machine.Pin.IN, machine.Pin.PULL_UP)

encoder_ticks = 0

def encoder_isr(pin):
    global encoder_ticks
    if ENC_B.value():
        encoder_ticks += 1
    else:
        encoder_ticks -= 1

ENC_A.irq(trigger=machine.Pin.IRQ_RISING, handler=encoder_isr)

speed = 26214

def set_speed(pct):
    global speed
    speed = int(65535 * pct / 100)

def lift_enable():
    REN.value(1)
    LEN.value(1)

def lift_disable():
    RPWM.duty_u16(0)
    LPWM.duty_u16(0)
    REN.value(0)
    LEN.value(0)

def lift_up():
    lift_enable()
    LPWM.duty_u16(0)
    RPWM.duty_u16(speed)

def lift_down():
    lift_enable()
    RPWM.duty_u16(0)
    LPWM.duty_u16(speed)

def lift_stop():
    lift_disable()

lift_disable()

print("READY")

poll = select.poll()
poll.register(sys.stdin, select.POLLIN)

while True:
    events = poll.poll(100)
    if events:
        line = sys.stdin.readline().strip()
        if line == "UP":
            lift_up()
            print("OK:UP")
        elif line == "DOWN":
            lift_down()
            print("OK:DOWN")
        elif line == "STOP":
            lift_stop()
            print("OK:STOP")
        elif line == "TICKS":
            print("TICKS:" + str(encoder_ticks))
        elif line == "PING":
            print("PONG")
        elif line.startswith("SPEED:"):
            try:
                pct = int(line.split(":")[1])
                pct = max(0, min(100, pct))
                set_speed(pct)
                print("OK:SPEED:" + str(pct))
            except ValueError:
                print("ERR:SPEED")
        else:
            print("ERR:UNKNOWN")
