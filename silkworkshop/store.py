"""进程内数据存储：实体表 + 追加式台账 + 幂等记录。"""

import threading


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.batches = {}
            self.pieces = {}
            self.movements = []  # 追加式流转台账
            self.plans = {}
            self.instructors = {}
            self.sessions = {}
            self.participants = {}
            self.enrollments = {}
            self.works = {}
            self.works_by_offline_ref = {}  # (session_id, offline_ref) -> Work，断网补录去重
            self.consents = {}
            self.publications = {}
            self.handovers = []
            self.fees = []
            self.settlements = {}  # session_id -> settlement
            self.idempotency = {}  # key -> {fingerprint, result}
            self.counters = {}

    def next_id(self, kind):
        self.counters[kind] = self.counters.get(kind, 0) + 1
        return f"{kind}-{self.counters[kind]:04d}"


STORE = Store()
