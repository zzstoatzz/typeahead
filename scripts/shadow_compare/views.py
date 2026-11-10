"""The comparable view of one actor, as every corpus source reports it."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Doc:
    sha: str | None
    fields: dict | None = None

    @property
    def state(self):
        if self.sha is None:
            return "absent"
        if self.fields is None:
            return "present"
        return "deleted" if self.fields.get("deleted") else "live"

    @property
    def live(self):
        return self.state in ("present", "live")

    def summary(self):
        return {"state": self.state, "document_sha256": self.sha, "fields": self.fields}


ABSENT = Doc(None)
