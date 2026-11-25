"""ASCII branding and feedback for the interactive terminal."""
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from itertools import count, groupby


MONOGRAM = (
    "       @@@@@@@@@@@@@@",
    "      @%%%%*******%%%@@",
    "      @%%%*       @%%+%@",
    "     @%%%*         @###*:.",
    "     @%%%*        @#+##*:..",
    "    @%%%*       @@###**::..",
    "    @%%%+@@@@@@@##***:::...",
    "   @####***##+###*::::....",
    "   @###*   @*###*::.....",
    "  @###*      @*##@@..",
    "  @###*        @#+#@@",
    " @****          @****@",
    "  :::::...       ::::::.",
    "    .....          ......",
)
CHARACTER_RAMP = " .:-=+*#%@"
TETHER_ROW = 7
MAX_TETHER = 4
SCAN_FRAMES = len(MONOGRAM) + 2
TRAIL = (("*", "gold"), ("o", "pink"), (".", "violet"), (".", "blue"))
WORDMARK = ("R I T T A I", "H A R N E S S")
COLORS = {
    # RGB, xterm-256 index, basic ANSI foreground
    "gold": ((255, 217, 133), 222, 93),
    "pink": ((252, 147, 215), 212, 95),
    "lilac": ((217, 182, 255), 183, 95),
    "violet": ((175, 136, 255), 141, 95),
    "blue": ((120, 154, 253), 111, 94),
    "cyan": ((98, 224, 247), 81, 96),
    "mint": ((143, 245, 196), 121, 92),
    "coral": ((255, 153, 160), 210, 91),
    "muted": ((157, 175, 202), 146, 37),
    "slate": ((104, 116, 180), 103, 34),
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
        title = self.style(WORDMARK[0], "lilac", bold=True) + " " + self.style(WORDMARK[1], "cyan", bold=True)
        hints = self.style("/help", "mint") + " for commands"
        if columns < 34:
            return [self.style("RittaiHarness", "lilac", bold=True), self.pulse(step), hints]

        wide = columns >= 64
        lines = [""]
        for row, shape in enumerate(MONOGRAM):
            cells = self.monogram_row(shape, row, step)
            line = "".join(self.style("".join(char for char, _ in group), tone, bold=tone in ("gold", "mint"))
                           for tone, group in groupby(cells, key=lambda cell: cell[1]))
            if wide and row in (5, 8):
                line += " " * (36 - len(cells)) + (title if row == 5 else hints)
            lines.append(line)
        if not wide:
            lines.extend((title, hints))
        lines.append("")
        return lines

    def monogram_row(self, shape, row, step):
        cells = [(" ", None)] * 2
        shadow, highlight = ("violet", "lilac") if row < TETHER_ROW else ("blue", "cyan")
        for char in shape:
            strength = CHARACTER_RAMP.index(char)
            if row >= step:
                cells.append((CHARACTER_RAMP[strength // 3], "slate"))
            elif strength <= 3:
                cells.append((char, "slate"))
            elif step == row + 1:
                cells.append((CHARACTER_RAMP[min(strength + 1, len(CHARACTER_RAMP) - 1)], "mint"))
            else:
                cells.append((char, highlight if strength >= 8 else shadow))

        if row == TETHER_ROW and row < step:
            length = min(MAX_TETHER, step - row)
            cable = [("-", "cyan")] * (length - 1) + [("o", "mint")]
            cells[5 - length:5] = reversed(cable)
            cells[8:11] = [(char, "gold") for char in "[=]"]
            cells.extend(cable)
        return cells

    def banner(self):
        columns, rows = shutil.get_terminal_size()
        if columns < 19 or rows < 6:
            self.write("RittaiHarness")
            return
        if rows < len(MONOGRAM) + 6:
            columns = min(columns, 33)
        last = SCAN_FRAMES - 1
        frames = range(last + 1) if self.animate else (last,)
        if self.animate:
            print("\033[?25l", end="", flush=True)
        try:
            for index, step in enumerate(frames):
                lines = self.banner_frame(step, columns)
                redraw = f"\033[{len(lines)}F\033[J" if index else ""
                print(redraw + "\n".join(lines), flush=True)
                if step < last:
                    time.sleep(0.055)
        finally:
            if self.animate:
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
        return self.style("[", "violet") + "".join(trail) + self.style("]", "violet")

    def write(self, text="", *, label="", tone="mint"):
        print(self.style(terminal_text(label), tone, bold=True) + terminal_text(text), flush=True)

    def prompt(self, attached, *, readline):
        prompt = "lead>" if attached else "rittai>"
        code = self.palette.get("mint" if attached else "lilac")
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
