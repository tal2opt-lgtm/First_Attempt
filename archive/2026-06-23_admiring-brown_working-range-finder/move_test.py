"""
Minimal isolation test -- move ONLY the bottom (vertical, M7) axis.

Purpose: settle the "algorithm vs controller" question once and for all.
This script has NO sensors, NO thickness module, NO status reads, NO
completion logic, NO threads, NO conditions.  All it does is poke the M7
command register (reg10) with a POSITIONS command a few times.

  -> If the HMI jumps to its entry screen while THIS runs, the screen-switch
     is the controller reacting to a PC write on reg10, and has nothing to do
     with our measurement code.
  -> If the HMI stays put, the trigger is something else our bigger program
     does (and we keep looking there).

Register map (same machine list):
    reg0   livesign  (1 = comm OK)
    reg10  M7 (bottom / vertical) command : 1..6 = Go Pos N, 4 = POS4 down-step

Safety: POSITIONS moves self-terminate in the controller (no JOG run-away).
POS4 lowers the part, so start the axis high enough and keep --steps small.
The command register is always cleared to 0 on exit.

Examples:
    python move_test.py                 # 5 x POS4 (down), livesign on
    python move_test.py --steps 1       # a single move
    python move_test.py --pos 1         # send POS1 instead
    python move_test.py --no-livesign   # test WITHOUT the comm heartbeat
"""

import argparse
import time

from pyModbusTCP.client import ModbusClient

REG_LIVESIGN = 0      # PC -> PLC : 1 = comm OK
REG_Y_CMD    = 10     # PC -> PLC : M7 (bottom/vertical) command
POS_STEP     = 4      # POS4 = relative down-step (interval set in the HMI)


def main():
    ap = argparse.ArgumentParser(description="Minimal M7 (bottom axis) move test")
    ap.add_argument("--host", default="192.168.3.169")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--pos", type=int, default=POS_STEP,
                    help="POS code to write to reg10 (4 = POS4 down-step)")
    ap.add_argument("--steps", type=int, default=5, help="how many moves to do")
    ap.add_argument("--move-sec", type=float, default=1.5,
                    help="time to let each move run before clearing the command")
    ap.add_argument("--gap-sec", type=float, default=1.0, help="idle time between moves")
    ap.add_argument("--no-livesign", action="store_true",
                    help="do NOT pulse reg0 (test whether the comm watchdog matters)")
    args = ap.parse_args()

    c = ModbusClient(host=args.host, port=args.port, auto_open=True,
                     auto_close=False, timeout=0.5)
    if not c.open():
        raise SystemExit(f"cannot connect to {args.host}:{args.port}")
    print(f"connected to {args.host}:{args.port}  (livesign={'off' if args.no_livesign else 'on'})")

    def livesign():
        if not args.no_livesign:
            c.write_single_register(REG_LIVESIGN, 1)

    def hold(sec):
        # Wait in small chunks, pulsing the livesign so the PLC comm watchdog
        # stays fed during the wait (use --no-livesign to remove this).
        t0 = time.time()
        while time.time() - t0 < sec:
            livesign()
            time.sleep(0.2)

    try:
        for i in range(1, args.steps + 1):
            print(f"[{i}/{args.steps}] reg10 = {args.pos}   (move) -> watch the HMI")
            livesign()
            c.write_single_register(REG_Y_CMD, args.pos)
            hold(args.move_sec)

            print(f"[{i}/{args.steps}] reg10 = 0    (idle) -> watch the HMI")
            c.write_single_register(REG_Y_CMD, 0)
            hold(args.gap_sec)
        print("done.")
    finally:
        c.write_single_register(REG_Y_CMD, 0)   # safety: leave the axis idle


if __name__ == "__main__":
    main()
