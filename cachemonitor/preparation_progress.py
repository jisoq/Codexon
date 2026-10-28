from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypeAlias


class PreparationStage(Enum):
    STARTING = 'starting'
    COLLECTING = 'collecting'
    CONVERTING = 'converting'
    PUBLISHING = 'publishing'


@dataclass(frozen=True)
class PreparationWork:
    stage: Literal[PreparationStage.COLLECTING, PreparationStage.CONVERTING]
    processed: int
    total: int


PreparationProgress: TypeAlias = PreparationStage | PreparationWork


@dataclass(frozen=True)
class PreparationPending:
    started_at: float
    progress: PreparationProgress


def preparation_loading(pending: PreparationPending, *, now: float) -> dict[str, object]:
    from .i18n import tr, formatted
    progress = pending.progress
    stage = progress.stage if isinstance(progress, PreparationWork) else progress
    title, detail = {
        PreparationStage.STARTING: ('사용 기록 준비 중', '저장된 사용 기록을 확인한 뒤 사용량을 표시합니다.'),
        PreparationStage.COLLECTING: ('사용 기록 수집 중', '저장된 Codex 사용 기록을 읽고 있습니다.'),
        PreparationStage.CONVERTING: ('사용량 계산 중', '확인한 기록으로 사용량을 계산하고 있습니다.'),
        PreparationStage.PUBLISHING: ('사용량 반영 중', '확인한 사용량을 화면에 반영하고 있습니다.'),
    }[stage]
    state = dict(active=True, retained=False, running=True, showStages=False,
                 stageName=stage.value, title=tr(title), detail=tr(detail),
                 elapsedText=formatted('{seconds}초 경과', seconds=max(0, int(now-pending.started_at))),
                 guidanceText=tr('준비하는 동안 다른 탭을 이용할 수 있습니다.'))
    if isinstance(progress, PreparationWork):
        template = ('확인한 파일 {count}개' if stage is PreparationStage.COLLECTING else '처리한 세션 {count}개')
        state.update(processed=progress.processed, total=progress.total,
                     progressText=formatted(template, count=f'{progress.processed:,} / {progress.total:,}'))
    return state
