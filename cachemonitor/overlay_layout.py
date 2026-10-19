"""Immutable logical geometry for the single-column session overlay."""
from dataclasses import dataclass

WIDTH = 300
MARGIN = 12
PREFERRED_HEIGHT = 720
MIN_SUB_HEIGHT = 80


@dataclass(frozen=True)
class ColumnLayout:
    width: int
    height: float
    header: float
    status: float
    total: float
    charts: tuple
    chart_height: float
    main: float
    main_height: float
    main_detail: float
    main_detail_height: float
    sub_heading: float
    sub_y: float
    sub_height: float

    @classmethod
    def create(cls, *, preferred_height=PREFERRED_HEIGHT, main_detail_height=0,
               has_subagents=False, has_status=False, reduced=False, available_height=None):
        total = 64 if has_status else 44
        chart_height = (60 if reduced else 78) * .7
        charts = tuple(total + 38 + lane * chart_height for lane in range(3))
        main = charts[-1] + chart_height + 12
        main_height = 76
        detail_y = main + main_height
        detail_height = max(0, main_detail_height)
        sub_heading = detail_y + detail_height + 12
        sub_y = sub_heading + 26
        minimum = sub_y + MIN_SUB_HEIGHT + MARGIN if has_subagents else detail_y + detail_height + MARGIN
        requested = max(minimum, preferred_height + detail_height) if has_subagents else minimum
        height = min(requested, max(minimum, available_height)) if available_height is not None else requested
        return cls(WIDTH, height, 12, 38, total, charts, chart_height, main, main_height,
                   detail_y, detail_height, sub_heading, sub_y,
                   max(MIN_SUB_HEIGHT, height - sub_y - MARGIN) if has_subagents else 0)

    def mapping(self):
        """Compatibility keys remain derived from this one geometry value."""
        return dict(header=self.header, status=self.status, total=self.total,
                    charts=self.charts, chart_height=self.chart_height,
                    main=self.main, main_height=self.main_height,
                    main_detail=self.main_detail, main_detail_height=self.main_detail_height,
                    sub_heading=self.sub_heading, sub_y=self.sub_y, sub_height=self.sub_height,
                    session=self.main, context=self.charts[0], recent=self.charts[0],
                    content_end=self.charts[-1]+self.chart_height,
                    body_end=self.height-MARGIN, height=self.height, full_height=self.height)
