"""ASCII branding and feedback for the interactive terminal."""
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from itertools import count, groupby


SHIELD = (
    r"   .--------------.",
    r"   |      /\      |",
    r"   |     /##\     |",
    r"   |    /----\    |",
    r"   \   /      \   /",
    r"    '._        _.'",
    r"       '.___.'",
)
ART_WIDTH = max(map(len, SHIELD)) + 4
SHINE_FRAMES = (ART_WIDTH + len(SHIELD) * 2) // 2
TWINKLE = (" ", ".", "+", "*", "+", ".", " ", "+")
TRAIL = (("*", "gold"), ("o", "shine"), (".", "blue"), (".", "steel"))
WORDMARK = "Aegis"
COLORS = {
    # RGB, xterm-256 index, basic ANSI foreground
    "gold": ((255, 200, 98), 221, 93),
    "blue": ((99, 166, 255), 75, 94),
    "steel": ((57, 106, 173), 61, 34),
    "shine": ((218, 235, 255), 195, 97),
    "cyan": ((98, 224, 247), 81, 96),
    "mint": ((143, 245, 196), 121, 92),
    "coral": ((255, 153, 160), 210, 91),
    "muted": ((157, 175, 202), 146, 37),
}
STATE_COLORS = {"queued": "gold", "running": "cyan", "completed": "mint", "failed": "coral", "cancelled": "muted"}
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
RESET = "\033[0m"
CLEAR_LINE = "\r\033[2K"


def terminal_text(value):
    return CONTROL.sub("", str(value))


class Terminal:
    def __init__(self, animation):
        term = os.environ.get("TERM", "")
        ansi = sys.stdout.isatty() and term not in ("", "dumb")
        self.animate = animation and ansi
        self.palette = {}
        if ansi and "NO_COLOR" not in os.environ:
            truecolor = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
            for name, (rgb, index, basic) in COLORS.items():
                if truecolor:
                    self.palette[name] = "38;2;" + ";".join(map(str, rgb))
                elif "256color" in term:
                    self.palette[name] = f"38;5;{index}"
                else:
                    self.palette[name] = str(basic)

    def style(self, text, tone, *, bold=False):
        code = self.palette.get(tone)
        if code and text.strip():
            return f"\033[{'1;' if bold else ''}{code}m{text}{RESET}"
        return text

    def banner_frame(self, step, columns):
        title = self.style(WORDMARK, "blue", bold=True)
        hints = self.style("/help", "blue") + " for commands"
        if columns < ART_WIDTH:
            return [title, hints]

        wide = columns >= ART_WIDTH + 4 + len("/help for commands")
        twinkle = TWINKLE[step - SHINE_FRAMES] if step >= SHINE_FRAMES else " "
        lines = [""]
        for row, shape in enumerate(SHIELD):
            cells = []
            for column, char in enumerate(shape):
                distance = abs(column + 2 * row - 2 * step)
                if step < SHINE_FRAMES and distance <= 2 and char != " ":
                    if not self.palette:
                        char = "@" if char == "#" else "#"
                    tone = "shine" if distance < 2 else "blue"
                else:
                    tone = "gold" if char in "|.'_-" else "blue"
                cells.append((char, tone))
            if row == 0:
                cells.extend((char, "gold") for char in twinkle.rjust(ART_WIDTH - len(shape)))
            line = "".join(self.style("".join(char for char, _ in group), tone, bold=True)
                           for tone, group in groupby(cells, key=lambda cell: cell[1]))
            if wide and row in (2, 4):
                line += " " * (ART_WIDTH + 4 - len(cells)) + (title if row == 2 else hints)
            lines.append(line)
        if not wide:
            lines.extend((title, hints))
        lines.append("")
        return lines

    def banner(self):
        columns, rows = shutil.get_terminal_size()
        if columns < len(WORDMARK) or rows < 4:
            self.write(WORDMARK)
            return
        if rows < len(SHIELD) + 6:
            columns = min(columns, ART_WIDTH - 1)
        last = SHINE_FRAMES + len(TWINKLE) - 1
        animate = self.animate and columns >= ART_WIDTH
        frames = range(last + 1) if animate else (last,)
        if animate:
            print("\033[?25l", end="", flush=True)
        try:
            for index, step in enumerate(frames):
                lines = self.banner_frame(step, columns)
                redraw = f"\033[{len(lines)}F\033[J" if index else ""
                print(redraw + "\n".join(lines), flush=True)
                if step < last:
                    time.sleep(0.045)
        finally:
            if animate:
                print("\033[?25h", end="", flush=True)

    def pulse(self, step):
        width = 12
        phase = step % (2 * width - 2)
        head = min(phase, 2 * width - 2 - phase)
        trail = [self.style("-", "blue") for _ in range(width)]
        for offset, (char, tone) in enumerate(TRAIL):
            column = head - offset if phase < width else head + offset
            if 0 <= column < width:
                trail[column] = self.style(char, tone, bold=offset == 0)
        return self.style("[", "steel") + "".join(trail) + self.style("]", "steel")

    def write(self, text="", *, label="", tone="mint"):
        print(self.style(terminal_text(label), tone, bold=True) + terminal_text(text), flush=True)

    def prompt(self, attached, *, readline):
        prompt = "lead>" if attached else "aegis>"
        code = self.palette.get("mint" if attached else "blue")
        if code and readline:
            return f"\001\033[1;{code}m\002{prompt}\001{RESET}\002 "
        return prompt + " "

    def status(self, session_id, state, stage, passed):
        prefix = self.style(terminal_text(session_id), "muted")
        status = self.style(terminal_text(state), STATE_COLORS.get(state, "cyan"), bold=True)
        progress = self.style(f"[{passed}/7 approved]", "mint" if passed == 7 else "blue")
        print(f"{prefix}  {status}  {terminal_text(stage)}  {progress}", flush=True)

    @contextmanager
    def waiting(self, label):
        columns = shutil.get_terminal_size().columns
        if not self.animate or columns < 17:
            self.write(label)
            yield time.sleep
            return

        frames = count()
        caption = terminal_text(label)[:max(0, columns - 17)]

        def pause(seconds):
            deadline = time.monotonic() + seconds
            while (remaining := deadline - time.monotonic()) > 0:
                print(f"\r{self.pulse(next(frames))}  {caption}", end="", flush=True)
                time.sleep(min(remaining, 0.08))
            print(CLEAR_LINE, end="", flush=True)

        try:
            yield pause
        finally:
            print(CLEAR_LINE, end="", flush=True)
