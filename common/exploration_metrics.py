"""Exploration is counted in deduplicated observed map cells, not world area."""
import math
from common.task_timing import number

DEFAULT_SURVEY_CELLS = 16
MAX_SURVEY_CELLS = 256
ROOM_SEARCH_CELLS = 16
LEGACY_CELL_AREA = 16  # Shipped 4x4 world-unit navigation grid.


def survey_cells(target):
    if 'cells' in target and 'area' in target:
        raise ValueError('Specify survey cells only; cells and legacy area cannot be combined')
    if 'area' in target:
        area=target['area']
        if type(area) is not int or not 16<=area<=4096:
            raise ValueError('Invalid legacy survey area (16-4096)')
        return math.ceil(area/LEGACY_CELL_AREA)
    cells=target.get('cells',DEFAULT_SURVEY_CELLS)
    if type(cells) is not int or not 1<=cells<=MAX_SURVEY_CELLS:
        raise ValueError('Survey cells must be an integer from 1 to 256')
    return cells


def observed_cells(region, cell_size=4):
    if 'observed_cells' in region:
        value=region['observed_cells']
        return value if type(value) is int and value>=0 else None
    # Accept older Mod snapshots during upgrades, without guessing missing facts.
    area=region.get('explored_area')
    if number(area) and area>=0 and number(cell_size) and cell_size>0:
        return math.floor(area/(cell_size*cell_size))
    return None
