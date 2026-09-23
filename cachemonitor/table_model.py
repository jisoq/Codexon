"""Qt Quick tables: lazy formatting, stable row anchors and observable selection."""
from collections import OrderedDict
import math
from PySide6.QtCore import QObject, Qt, QAbstractTableModel, QModelIndex, Signal, Property, Slot, QTimer
from PySide6.QtGui import QColor
from .presentation import Node, ScrollPosition


class Cell:
    def __init__(self,text=''):
        self._text=str(text);self._tooltip=str(text);self._data={};self._background='';self.owner=None
    def text(self):return self._text
    def toolTip(self):return self._tooltip
    def setText(self,text):
        if self._text!=str(text):self._text=str(text);self.notify()
    def setToolTip(self,text):
        if self._tooltip!=str(text):self._tooltip=str(text);self.notify()
    def setTextAlignment(self,value):self._data[Qt.TextAlignmentRole]=int(value)
    def setData(self,role,value):self._data[role]=value
    def data(self,role):return self._data.get(role)
    def setBackground(self,value):
        color=QColor(value).name()
        if self._background!=color:self._background=color;self.notify()
    def notify(self):
        if self.owner:self.owner.invalidate()


class Header:
    Interactive=0
    Stretch=1
    ResizeToContents=2
    def __init__(self,owner):self.owner=owner;self.sectionResized=owner.columnResized
    def setDefaultSectionSize(self,size):self.owner.put(rowHeight=size)
    def defaultSectionSize(self):return self.owner._state['rowHeight']
    def length(self):return sum(self.owner._state['widths'])
    def visualIndex(self,col):return self.owner._order.index(col)
    def logicalIndex(self,index):return self.owner._order[index]
    def moveSection(self,old,new):
        order=self.owner._order;order.insert(new,order.pop(old));self.owner.invalidate();self.owner.geometryChanged.emit()
    def setSectionResizeMode(self,*args):self.owner.put(autoWidths=len(args)==1 and args[0]==self.ResizeToContents)
    def setMinimumSectionSize(self,value):self.owner.put(minColumnWidth=value)
    def setDefaultAlignment(self,value):self.owner.put(headerAlignment=int(value))


class Rows(QAbstractTableModel):
    def __init__(self,owner,headers):
        super().__init__(owner);self.owner=owner;self.headers=list(headers);self.rows=[]
        self.formatter=None;self.cache=OrderedDict();self.formatted=0
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.rows)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.headers)
    def roleNames(self):return {Qt.DisplayRole:b'display',Qt.ToolTipRole:b'tooltip',Qt.BackgroundRole:b'cellBackground',Qt.TextAlignmentRole:b'alignment',Qt.UserRole+1:b'changedCell',Qt.UserRole+2:b'cacheZero',Qt.UserRole+3:b'cellBar'}
    def headerData(self,section,orientation,role=Qt.DisplayRole):
        if orientation==Qt.Horizontal and role==Qt.DisplayRole and section<len(self.headers):return self.headers[self.owner._order[section]]
    def data(self,index,role=Qt.DisplayRole):
        if not index.isValid() or not 0<=index.row()<len(self.rows) or not 0<=index.column()<len(self.owner._order):return None
        row=index.row();col=self.owner._order[index.column()]
        if role==Qt.UserRole+1:return self.owner.changes.strength(row,col)
        if role==Qt.UserRole+2:
            record=self.rows[row]
            value=record.get('cached') if isinstance(record,dict) else None
            inp=record.get('input') if isinstance(record,dict) else None
            return bool(self.owner._state.get('highlightZeroCache') and type(inp) is int and inp>0 and type(value) is int and value==0)
        if role==Qt.UserRole+3:
            record=self.rows[row]
            bars=record.get('bars',[]) if isinstance(record,dict) else []
            value=bars[col] if col<len(bars) else None
            return max(0.,min(1.,value)) if type(value) in (int,float) and math.isfinite(value) else -1.
        if role==Qt.BackgroundRole:return '' if self.formatter else self.rows[row][col]._background if self.rows[row][col] else ''
        if role==Qt.TextAlignmentRole:
            cell=self.rows[row][col] if not self.formatter else None
            return (cell.data(role) if cell and cell.data(role) is not None else int((Qt.AlignLeft if col in self.owner._state.get('leftColumns',[0]) else Qt.AlignRight)|Qt.AlignVCenter))
        if role not in (Qt.DisplayRole,Qt.ToolTipRole):return None
        if not self.formatter:
            cell=self.rows[row][col]
            return (cell.toolTip() if role==Qt.ToolTipRole else cell.text()) if cell else ''
        key=(row,col,role)
        if key not in self.cache:
            self.cache[key]=self.formatter(self.rows[row],col,role);self.formatted+=1
            if len(self.cache)>4096:self.cache.popitem(last=False)
        return self.cache[key]
    def replace(self,rows,formatter):
        if rows is self.rows and formatter==self.formatter:return False
        # Preserve the Quick table and its header while filters change row counts.
        # Repeated resets through zero can invalidate their shared GPU delegates.
        old_count=len(self.rows);new_count=len(rows)
        if new_count<old_count:self.beginRemoveRows(QModelIndex(),new_count,old_count-1)
        elif new_count>old_count:self.beginInsertRows(QModelIndex(),old_count,new_count-1)
        same_formatter=formatter==self.formatter
        changed={i for i in range(min(old_count,new_count)) if rows[i]!=self.rows[i]} if same_formatter else set(range(new_count))
        self.rows=rows;self.formatter=formatter
        if same_formatter:
            self.cache=OrderedDict((k,v) for k,v in self.cache.items() if k[0] not in changed and k[0]<new_count)
        else:self.cache.clear()
        if new_count<old_count:self.endRemoveRows()
        elif new_count>old_count:self.endInsertRows()
        if changed and self.headers:self.dataChanged.emit(self.index(min(changed),0),self.index(min(max(changed),new_count-1),len(self.headers)-1))
        return True


class Table(Node):
    kind='table'
    geometryChanged=Signal()
    columnResized=Signal(int,int,int)
    itemSelectionChanged=Signal()
    cellClicked=Signal(int,int)
    cellActivated=Signal(int,int)
    scrollRequested=Signal(int,int)
    def __init__(self,rows=0,columns=0,parent=None,headers=None):
        super().__init__(parent);headers=headers or ['']*columns
        self._order=list(range(len(headers)));self._model=Rows(self,headers)
        self._header_cells={}
        self._header=Header(self);self._vertical=ScrollPosition(self);self._horizontal=ScrollPosition(self)
        self._selected=-1;self.live_update=False;self.first_visible=0;self.last_visible=-1
        self._invalidate_timer=QTimer(self);self._invalidate_timer.setSingleShot(True)
        self._invalidate_timer.timeout.connect(self.flush_changes)
        self.put(headers=headers,widths=[180]*len(headers),rowHeight=30,selected=-1,minHeight=170)
        from .change_highlight import ChangeHighlight
        ChangeHighlight(self)
        if rows:self.setRowCount(rows)
    @Property(QObject,constant=True)
    def rowModel(self):return self._model
    @Property(QObject,constant=True)
    def verticalPosition(self):return self._vertical
    @Property(QObject,constant=True)
    def horizontalPosition(self):return self._horizontal
    def put(self,**values):
        geometry=any(key in values and self._state.get(key)!=values[key] for key in ("widths","rowHeight"))
        super().put(**values)
        if geometry:self.geometryChanged.emit()
    def model(self):return self._model
    def rowCount(self):return self._model.rowCount()
    def columnCount(self):return self._model.columnCount()
    def horizontalHeader(self):return self._header
    def verticalHeader(self):return self._header
    def verticalScrollBar(self):return self._vertical
    def horizontalScrollBar(self):return self._horizontal
    def currentRow(self):return self._selected
    def currentIndex(self):return self._model.index(self._selected,0)
    def frameWidth(self):return 0
    def setColumnCount(self,count):self.setHorizontalHeaderLabels(['']*count)
    def setColumnWidth(self,col,width):
        widths=list(self._state['widths']);old=widths[col];widths[col]=width
        self.put(widths=widths);self.columnResized.emit(col,old,width)
    @Slot(int,float)
    def resizeColumn(self,visual,width):self.setColumnWidth(self._order[visual],max(45,round(width)))
    @Slot(int,result=float)
    def visualColumnWidth(self,visual):
        return self._state['widths'][self._order[visual]] if 0<=visual<len(self._order) else 100
    @Slot(int,result=int)
    def logicalColumn(self,visual):return self._order[visual] if 0<=visual<len(self._order) else -1
    def columnWidth(self,col):return self._state['widths'][col]
    def setHorizontalHeaderLabels(self,headers):
        if self._model.headers==list(headers):return
        if len(self._order)!=len(headers):
            self._model.beginResetModel();self._order=list(range(len(headers)))
            self._model.headers=list(headers);self._model.rows=[];self._model.endResetModel();self.put(widths=[180]*len(headers))
        else:self._model.headers=list(headers)
        self.put(headers=list(headers));self._model.headerDataChanged.emit(Qt.Horizontal,0,len(headers)-1)
    def horizontalHeaderItem(self,col):
        if col not in self._header_cells:
            self._header_cells[col]=Cell(self._model.headers[col]);self._header_cells[col].owner=self
        self._header_cells[col]._text=self._model.headers[col]
        return self._header_cells[col]
    @Slot(int,result=str)
    def headerTip(self,visual):
        return self.horizontalHeaderItem(self._order[visual]).toolTip()
    @Slot(int,int,result=str)
    def cellDetails(self,row,visual):
        if not 0 <= visual < self.columnCount():return ''
        cell=self.item(row,self._order[visual])
        if cell is None:return ''
        title=self._model.headers[self._order[visual]]
        detail=cell.toolTip()
        definition=self.headerTip(visual)
        return (f'{title}\n{cell.text()}'
                + (f'\n\n{detail}' if detail and detail!=cell.text() else '')
                + (f'\n\n{definition}' if definition and definition!=title and definition not in detail else ''))
    def setRowCount(self,count):
        old=self._model.rows if self._model.formatter is None else []
        self._model.replace(old[:count]+[[None]*self.columnCount() for _ in range(max(0,count-len(old)))],None)
        if self._selected>=count:self._selected=-1;self.put(selected=-1)
    def setItem(self,row,col,cell):cell.owner=self;self._model.rows[row][col]=cell;self.invalidate()
    def item(self,row,col):
        if not 0<=row<self.rowCount() or not 0<=col<self.columnCount():return None
        if self._model.formatter:
            cell=Cell(self._model.formatter(self._model.rows[row],col,Qt.DisplayRole))
            cell.setToolTip(self._model.formatter(self._model.rows[row],col,Qt.ToolTipRole));return cell
        return self._model.rows[row][col]
    def invalidate(self):
        self._model.cache.clear()
        self._invalidate_timer.start(0)
    def flush_changes(self):
        if self.rowCount() and self.columnCount():self._model.dataChanged.emit(self._model.index(0,0),self._model.index(self.rowCount()-1,self.columnCount()-1))
    def refresh_highlights(self):
        first=max(0,self.first_visible);last=min(self.rowCount()-1,self.last_visible)
        if first<=last and self.columnCount():
            self._model.dataChanged.emit(self._model.index(first,0),self._model.index(last,self.columnCount()-1),[Qt.UserRole+1])
    @Slot(int)
    def selectRow(self,row):
        if row!=self._selected:self._selected=row;self.put(selected=row);self.itemSelectionChanged.emit()
    def select_row(self,row):
        blocked=self.blockSignals(True);self.selectRow(row);self.blockSignals(blocked);self.changed.emit()
    @Slot(int,int)
    def click(self,row,col):self.selectRow(row);self.cellClicked.emit(row,self._order[col])
    @Slot(int)
    @Slot(int,int)
    def activateRow(self,row,column=0):
        if 0<=row<self.rowCount() and 0<=column<self.columnCount():
            self.selectRow(row);self.cellActivated.emit(row,self._order[column])
    @Slot(int,int)
    def visibleRows(self,first,last):self.first_visible=first;self.last_visible=last
    def scrollTo(self,index,*args):self.scrollRequested.emit(index.row(),index.column())
    @staticmethod
    def row_key(row):
        if isinstance(row,dict):
            if 'session' in row:return row['session']['home'],row['session']['id']
            return row.get('home'),row.get('sid'),row.get('key') or row.get('turn') or row.get('id')
        if isinstance(row,list):return (row[0].text() if hasattr(row[0],'text') else row[0]) if row else ''
        return row
    def set_rows(self,rows,formatter):
        if rows is self._model.rows and formatter==self._model.formatter:return
        previous=self.changes.before();old=self._model.rows;top=self.first_visible
        anchor=self.row_key(old[top]) if 0<=top<len(old) else None
        selected=self.row_key(old[self._selected]) if 0<=self._selected<len(old) else None
        height=self._state['rowHeight'];offset=self._vertical.value()-top*height
        self._model.replace(rows,formatter)
        if anchor is not None:
            new=next((i for i,row in enumerate(rows) if self.row_key(row)==anchor),None)
            if new is not None:self._vertical.setValue(new*height+offset)
        if selected is not None:self.select_row(next((i for i,row in enumerate(rows) if self.row_key(row)==selected),-1))
        self.changes.after(previous)


class LazyTable(Table):
    def __init__(self,headers):super().__init__(headers=headers)
