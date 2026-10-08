from dataclasses import dataclass


@dataclass(frozen=True)
class DialogueRow:
    row_number: int
    episode: str
    character: str
    start_ms: int
    end_ms: int
    text: str
    voice_id: str

    @property
    def group(self):
        return self.episode, self.character
