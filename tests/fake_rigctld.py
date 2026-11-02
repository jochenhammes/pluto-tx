"""A rigctld for tests: the extended protocol over TCP, in a thread, with what a real X6200 was measured to do
(docs/x6200/TESTS.md): a PTT change reads back late, and a power write is applied but answered with error -5."""
import socketserver
import threading
import time


class FakeRigctld:
    def __init__(self, ptt_delay_s=0.2, frequency=14_220_600.0, mode="USB", rf_power=0.012):
        self.ptt_delay_s = ptt_delay_s
        self.frequency, self.mode, self.rf_power = frequency, mode, rf_power
        self.commanded_ptt = False
        self.ptt_changed_at = 0.0
        self.refuse_ptt = False       # the firmware's tx_refused()
        self.stuck_frequency = False
        self.stuck_power = False
        self.drop_next = False
        self.silent = False
        self.stale_reads_after_write = 0   # reads that still give the old value after a write, as seen on the real radio
        self.stale_power_reads = 0
        self.old_power = None
        self.log = []                 # (monotonic time, command) of every request
        self.lock = threading.Lock()
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    for raw in self.rfile:
                        line = raw.decode().strip()
                        if owner.drop_next:
                            owner.drop_next = False
                            return
                        if owner.silent:
                            continue
                        self.wfile.write(owner.answer(line.lstrip("+")).encode())
                except OSError:  # the client went away, which tests do on purpose
                    pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def ptt(self):
        """What the radio reports."""
        with self.lock:
            if time.monotonic() - self.ptt_changed_at >= self.ptt_delay_s:
                return self.commanded_ptt
            return not self.commanded_ptt

    def commands(self, prefix):
        return [c for _t, c in self.log if c.startswith(prefix)]

    def answer(self, line):
        self.log.append((time.monotonic(), line))
        word = line.split()
        cmd = word[0] if word else ""
        if cmd == "f":
            return f"get_freq:\nFrequency: {self.frequency:.0f}\nRPRT 0\n"
        if cmd == "F":
            if not self.stuck_frequency:
                self.frequency = float(word[1])
            return f"set_freq: {word[1]}\nRPRT 0\n"
        if cmd == "m":
            return f"get_mode:\nMode: {self.mode}\nPassband: 2800\nRPRT 0\n"
        if cmd == "M":
            self.mode = word[1]
            return f"set_mode: {word[1]} {word[2]}\nRPRT 0\n"
        if cmd == "t":
            return f"get_ptt:\nPTT: {int(self.ptt)}\nRPRT 0\n"
        if cmd == "T":
            on = word[1] != "0"
            if on and self.refuse_ptt:
                return f"set_ptt: {word[1]}\nRPRT 0\n"  # acknowledged, but the state never changes
            with self.lock:
                if on != self.commanded_ptt:
                    self.ptt_changed_at = time.monotonic()
                self.commanded_ptt = on
            return f"set_ptt: {word[1]}\nRPRT 0\n"
        if cmd == "l":
            value = {"RFPOWER": self.rf_power, "SWR": 1.2, "ALC": 0.0, "STRENGTH": -29}[word[1]]
            if word[1] == "RFPOWER" and self.stale_power_reads > 0:
                self.stale_power_reads -= 1
                value = self.old_power
            return f"get_level: {word[1]}\n{value}\nRPRT 0\n"
        if cmd == "L" and word[1] == "RFPOWER":
            self.old_power = self.rf_power
            self.stale_power_reads = self.stale_reads_after_write
            if not self.stuck_power:
                self.rf_power = max(3, round(float(word[2]) * 255)) / 255  # the real radio goes no lower than 3/255
            return f"set_level: RFPOWER {word[2]}\nRPRT -5\n"
        return "RPRT -11\n"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
