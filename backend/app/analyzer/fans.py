"""FanKnowledge: learns each fan's RPM range so 'RPM / highest RPM seen' can stand in for the duty cycle.

Laptop embedded controllers expose RPM but not duty. "Fan at ~100%" therefore has to be *estimated*.
That is only meaningful once we have seen the fan spin both slowly and fast; before that, a constant
RPM would trivially equal its own maximum. ``trusted`` encodes that guard.
"""

from __future__ import annotations

import json


class FanKnowledge:
    def __init__(self, initial: dict[str, tuple[float, float]] | None = None) -> None:
        self._range: dict[str, list[float]] = {k: [v[0], v[1]] for k, v in (initial or {}).items()}
        self.dirty = False

    def observe(self, name: str, rpm: float | None) -> None:
        if rpm is None or rpm < 0:
            return
        r = self._range.get(name)
        if r is None:
            self._range[name] = [rpm, rpm]
            self.dirty = True
            return
        if rpm < r[0]:
            r[0], self.dirty = rpm, True
        if rpm > r[1]:
            r[1], self.dirty = rpm, True

    def max_rpm(self, name: str) -> float | None:
        r = self._range.get(name)
        return r[1] if r else None

    def min_rpm(self, name: str) -> float | None:
        r = self._range.get(name)
        return r[0] if r else None

    def ratio(self, name: str, rpm: float) -> float | None:
        mx = self.max_rpm(name)
        return rpm / mx if mx and mx > 0 else None

    def position(self, name: str, rpm: float) -> float | None:
        """0.0 = slowest speed ever seen, 1.0 = fastest. None until the fan's range is known."""
        r = self._range.get(name)
        if not r or r[1] <= r[0]:
            return None
        return max(0.0, min(1.0, (rpm - r[0]) / (r[1] - r[0])))

    def trusted(self, name: str, min_span: float, min_max_rpm: float = 1000.0) -> bool:
        """True once the fan has been seen at clearly different speeds, so its 'maximum' means something."""
        r = self._range.get(name)
        if not r or r[1] < min_max_rpm:
            return False
        return (r[1] - r[0]) / r[1] >= min_span

    # ---- persistence (stored in the settings table) -----------------------------------------------

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in self._range.items()})

    @classmethod
    def from_json(cls, raw: str | None) -> FanKnowledge:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            return cls({str(k): (float(v[0]), float(v[1])) for k, v in data.items()})
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return cls()  # a damaged value must never stop the engine
