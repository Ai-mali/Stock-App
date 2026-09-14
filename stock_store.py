"""Excel-backed store for the Daikin Stock Tracker.

One workbook (daikin_stock.xlsx, next to the app) holds:
  MasterRecord — one row per physical unit:
      Supplier | Type | Model | Serial | Date In | Status | Customer | Date Out
  Types        — the Type list (Wall Mount, Cassette, ...)
  ModelTypes   — exact full Model string -> Type (never prefix matching)
"""

from pathlib import Path

from openpyxl import Workbook, load_workbook

DB_PATH = Path(__file__).with_name("daikin_stock.xlsx")

RECORD_HEADER = ["Supplier", "Type", "Model", "Serial",
                 "Date In", "Status", "Customer", "Date Out"]


class StockStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.wb = None
        self.recs = self.types_sheet = self.map_sheet = None
        self.records: list[dict] = []          # parsed MasterRecord rows
        self.types: list[str] = []             # ordered Type names
        self.model_to_type: dict[str, str] = {}  # UPPER(model) -> Type
        self._serial_set: set[str] = set()     # lowercase serials
        self.load()

    # ---------------------------------------------------------- load/save
    def load(self):
        if self.path.exists():
            self.wb = load_workbook(self.path)
        else:
            self.wb = Workbook()
            self.wb.active.title = "MasterRecord"
        self.recs = self._sheet("MasterRecord", RECORD_HEADER)
        self.types_sheet = self._sheet("Types", ["Type"])
        self.map_sheet = self._sheet("ModelTypes", ["Model", "Type"])

        self.records, self.types, self.model_to_type, self._serial_set = \
            [], [], {}, set()
        for row in self.recs.iter_rows(min_row=2, values_only=True):
            if not row or row[3] in (None, ""):
                continue
            rec = dict(zip(RECORD_HEADER, ("" if v is None else v for v in row)))
            self.records.append(rec)
            self._serial_set.add(str(rec["Serial"]).strip().lower())
        for row in self.types_sheet.iter_rows(min_row=2, values_only=True):
            if row and row[0] and row[0] not in self.types:
                self.types.append(str(row[0]))
        for row in self.map_sheet.iter_rows(min_row=2, values_only=True):
            if row and row[0] and row[1]:
                self.model_to_type[str(row[0]).upper()] = str(row[1])
        self.save()

    def _sheet(self, name: str, header: list[str]):
        ws = (self.wb[name] if name in self.wb.sheetnames
              else self.wb.create_sheet(name))
        first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True),
                     None)
        if first is None or all(v is None for v in first):
            # written cell-by-cell: append() would leave an empty leading row
            for col, title in enumerate(header, start=1):
                ws.cell(row=1, column=col, value=title)
        return ws

    def save(self):
        self.wb.save(self.path)

    # ---------------------------------------------------------- queries
    def type_for(self, model: str) -> str:
        return self.model_to_type.get(model.strip().upper(), "")

    def find_dupes(self, serials: list[str]) -> list[str]:
        return [s for s in serials
                if s.strip().lower() in self._serial_set]

    # ---------------------------------------------------------- mutations
    def stock_in(self, supplier: str, model: str, serials: list[str],
                 date_in: str):
        """Shared save path for manual entry and scanned rows.

        Returns (added, dupes, ok_type):
          added  — serials written (one MasterRecord row each)
          dupes  — serials skipped because they already exist
          ok_type — False when the Model is unknown and no save happened;
                    UI must collect a Type then call stock_in_with_type().
        """
        serials = [s.strip() for s in serials if s.strip()]
        if not supplier.strip() or not model.strip() or not serials:
            return [], serials, True
        dupes, new_serials = self._split_dupes(serials)
        type_name = self.type_for(model)
        if not type_name:
            return [], dupes, False
        self._write_rows(supplier.strip(), model.strip(), type_name,
                         new_serials, date_in)
        return new_serials, dupes, True

    def stock_in_with_type(self, supplier: str, model: str, serials: list[str],
                           date_in: str, type_name: str):
        """Completes stock_in for a Model the user just assigned a Type."""
        type_name = type_name.strip()
        if not type_name:
            return [], serials
        self.assign_type(model, type_name)
        dupes, new_serials = self._split_dupes(
            [s.strip() for s in serials if s.strip()])
        self._write_rows(supplier.strip(), model.strip(), type_name,
                         new_serials, date_in)
        return new_serials, dupes

    def _split_dupes(self, serials):
        """Known serials AND repeated serials inside the same batch are dupes.
        Returns (dupes, new_serials) preserving order."""
        seen, dupes, new_serials = set(), [], []
        for s in serials:
            if s.lower() in seen or s.lower() in self._serial_set:
                dupes.append(s)
            else:
                new_serials.append(s)
            seen.add(s.lower())
        return dupes, new_serials

    def _write_rows(self, supplier, model, type_name, serials, date_in):
        for s in serials:
            self.recs.append([supplier, type_name, model, s, date_in,
                              "In Stock", "", ""])
            self.records.append({"Supplier": supplier, "Type": type_name,
                                 "Model": model, "Serial": s,
                                 "Date In": date_in, "Status": "In Stock",
                                 "Customer": "", "Date Out": ""})
            self._serial_set.add(s.lower())
        if serials:
            self.save()

    # ---------------------------------------------------------- types
    def assign_type(self, model: str, type_name: str):
        """Remember Model -> Type permanently (exact full string match)."""
        if type_name not in self.types:
            self.types.append(type_name)
            self.types_sheet.append([type_name])
        key = model.strip().upper()
        if self.model_to_type.get(key) != type_name:
            self.model_to_type[key] = type_name
            self.map_sheet.append([model.strip(), type_name])
        self.save()

    def add_type(self, type_name: str) -> bool:
        type_name = type_name.strip()
        if not type_name or type_name in self.types:
            return False
        self.types.append(type_name)
        self.types_sheet.append([type_name])
        self.save()
        return True
