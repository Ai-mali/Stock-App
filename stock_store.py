"""Excel-backed store for the AC Stock Tracker.

One workbook (daikin_stock.xlsx, next to the app) holds:
  MasterRecord — one row per physical unit:
      Supplier | Brand | Model | Serial | Date In | Status | Customer | Date Out
  Brands       — the brand list (Daikin, LG, Panasonic, ...)
  ModelBrands  — exact full Model string -> Brand (never prefix matching)
  Returns      — Serial | Model | Customer | Reason | Condition | Notes |
                 Action | Date

Statuses: "In Stock" | "Sold" | "Quarantined".
A restocked unit goes back to "In Stock"; a quarantined unit stays out of
both Available Stock and the Track List but keeps its record.

Existing files from the Type-era schema are migrated automatically:
sheet Types -> Brands, ModelTypes -> ModelBrands, header Type -> Brand.
"""

from pathlib import Path

from openpyxl import Workbook, load_workbook

DB_PATH = Path(__file__).with_name("daikin_stock.xlsx")

RECORD_HEADER = ["Supplier", "Brand", "Model", "Serial",
                 "Date In", "Status", "Customer", "Date Out"]
RETURN_HEADER = ["Serial", "Model", "Customer", "Reason",
                 "Condition", "Notes", "Action", "Date"]

BRAND_COLORS = ["#26d07c", "#3b82f6", "#f59e0b", "#a78bfa",
                "#f87171", "#38bdf8", "#fb923c", "#4ade80"]

IN_STOCK = "In Stock"
SOLD = "Sold"
QUARANTINED = "Quarantined"


class StockStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.wb = None
        self.recs = self.brands_sheet = self.map_sheet = self.ret_sheet = None
        self.records: list[dict] = []            # parsed MasterRecord rows
        self.brands: list[str] = []              # ordered Brand names
        self.model_to_brand: dict[str, str] = {}  # UPPER(model) -> Brand
        self.returns: list[dict] = []            # parsed Returns rows
        self._serial_set: set[str] = set()       # lowercase serials
        self.load()

    # ---------------------------------------------------------- load/save
    def load(self):
        if self.path.exists():
            self.wb = load_workbook(self.path)
        else:
            self.wb = Workbook()
            self.wb.active.title = "MasterRecord"
        self._migrate_type_schema()
        self.recs = self._sheet("MasterRecord", RECORD_HEADER)
        self.brands_sheet = self._sheet("Brands", ["Brand"])
        self.map_sheet = self._sheet("ModelBrands", ["Model", "Brand"])
        self.ret_sheet = self._sheet("Returns", RETURN_HEADER)

        self.records, self.brands, self.model_to_brand = [], [], {}
        self.returns, self._serial_set = [], set()
        for idx, row in enumerate(
                self.recs.iter_rows(min_row=2, values_only=True), start=2):
            if not row or row[3] in (None, ""):
                continue
            rec = dict(zip(RECORD_HEADER,
                           ("" if v is None else v for v in row)))
            rec["_row"] = idx  # Excel row, so updates hit the right cells
            self.records.append(rec)
            self._serial_set.add(str(rec["Serial"]).strip().lower())
        for row in self.brands_sheet.iter_rows(min_row=2, values_only=True):
            if row and row[0] and row[0] not in self.brands:
                self.brands.append(str(row[0]))
        for row in self.map_sheet.iter_rows(min_row=2, values_only=True):
            if row and row[0] and row[1]:
                self.model_to_brand[str(row[0]).upper()] = str(row[1])
        for row in self.ret_sheet.iter_rows(min_row=2, values_only=True):
            if not row or row[0] in (None, ""):
                continue
            self.returns.append(dict(
                zip(RETURN_HEADER, ("" if v is None else v for v in row))))
        self.save()

    def _migrate_type_schema(self):
        """Rename the Type-era sheets/column to Brand once, in place."""
        names = self.wb.sheetnames
        if "Types" in names and "Brands" not in names:
            self.wb["Types"].title = "Brands"
        if "ModelTypes" in names and "ModelBrands" not in names:
            self.wb["ModelTypes"].title = "ModelBrands"
        if "MasterRecord" in names:
            hdr = self.wb["MasterRecord"].cell(row=1, column=2).value
            if str(hdr or "").strip() == "Type":
                self.wb["MasterRecord"].cell(row=1, column=2, value="Brand")

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
    def brand_for(self, model: str) -> str:
        return self.model_to_brand.get(model.strip().upper(), "")

    def find_dupes(self, serials: list[str]) -> list[str]:
        return [s for s in serials
                if s.strip().lower() in self._serial_set]

    def _brand_color(self, name: str) -> str:
        try:
            return BRAND_COLORS[self.brands.index(name) % len(BRAND_COLORS)]
        except ValueError:
            return BRAND_COLORS[len(self.brands) % len(BRAND_COLORS)]

    def inventory(self) -> dict:
        """In-stock units grouped Brand -> Model -> serials (UI shape)."""
        inv: dict[str, dict] = {}
        for rec in self.records:
            if str(rec["Status"]).strip() != IN_STOCK:
                continue
            model = str(rec["Model"]).strip()
            brand = str(rec["Brand"]).strip() or "UNBRANDED"
            b = inv.setdefault(brand, {"color": self._brand_color(brand),
                                       "open": True, "models": {}})
            m = b["models"].setdefault(
                model, {"dateIn": str(rec["Date In"]), "serials": []})
            m["serials"].append(str(rec["Serial"]))
        # keep zero-stock brands visible too
        for brand in self.brands:
            inv.setdefault(brand, {"color": self._brand_color(brand),
                                   "open": True, "models": {}})
        for b in inv.values():
            for i, m in enumerate(sorted(b["models"]), start=1):
                b["models"][m]["idx"] = f"{i:02d}"
        return dict(sorted(inv.items()))

    def track_list(self) -> list[dict]:
        """Sold units, newest first."""
        out = [{"model": str(r["Model"]), "serial": str(r["Serial"]),
                "dateIn": str(r["Date In"]), "dateOut": str(r["Date Out"]),
                "customer": str(r["Customer"]), "status": str(r["Status"])}
               for r in self.records if str(r["Status"]).strip() == SOLD]
        return out

    def returns_list(self) -> list[dict]:
        return [{"serial": str(r["Serial"]), "model": str(r["Model"]),
                 "customer": str(r["Customer"]), "reason": str(r["Reason"]),
                 "condition": str(r["Condition"]), "notes": str(r["Notes"]),
                 "action": str(r["Action"]), "date": str(r["Date"])}
                for r in self.returns]

    # ---------------------------------------------------------- mutations
    def stock_in(self, supplier: str, model: str, serials: list[str],
                 date_in: str):
        """Shared save path for manual entry and scanned rows.

        Returns (added, dupes, ok_brand):
          added  — serials written (one MasterRecord row each)
          dupes  — serials skipped because they already exist
          ok_brand — False when the Model is unknown and no save happened;
                     UI must collect a Brand then call stock_in_with_brand().
        """
        serials = [s.strip() for s in serials if s.strip()]
        if not supplier.strip() or not model.strip() or not serials:
            return [], serials, True
        dupes, new_serials = self._split_dupes(serials)
        brand = self.brand_for(model)
        if not brand:
            return [], dupes, False
        self._write_rows(supplier.strip(), model.strip(), brand,
                         new_serials, date_in)
        return new_serials, dupes, True

    def stock_in_with_brand(self, supplier: str, model: str,
                            serials: list[str], date_in: str, brand: str):
        """Completes stock_in for a Model the user just assigned a Brand."""
        brand = brand.strip()
        if not brand:
            return [], serials
        self.assign_brand(model, brand)
        dupes, new_serials = self._split_dupes(
            [s.strip() for s in serials if s.strip()])
        self._write_rows(supplier.strip(), model.strip(), brand,
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

    def _write_rows(self, supplier, model, brand, serials, date_in):
        for s in serials:
            self.recs.append([supplier, brand, model, s, date_in,
                              IN_STOCK, "", ""])
            self.records.append({"Supplier": supplier, "Brand": brand,
                                 "Model": model, "Serial": s,
                                 "Date In": date_in, "Status": IN_STOCK,
                                 "Customer": "", "Date Out": "",
                                 "_row": self.recs.max_row})
            self._serial_set.add(s.lower())
        if serials:
            self.save()

    def stock_out(self, serials: list[str], customer: str, date_out: str):
        """Mark units Sold with one Customer + Date Out for the whole cart.

        Matches on Serial (case-insensitive) among In Stock rows only.
        Returns the serials actually marked.
        """
        wanted = {s.strip().lower() for s in serials if s.strip()}
        done = []
        for rec in self.records:
            if (str(rec["Serial"]).strip().lower() not in wanted
                    or str(rec["Status"]).strip() != IN_STOCK):
                continue
            rec["Status"] = SOLD
            rec["Customer"] = customer
            rec["Date Out"] = date_out
            row = rec["_row"]
            self.recs.cell(row=row, column=6, value=SOLD)
            self.recs.cell(row=row, column=7, value=customer)
            self.recs.cell(row=row, column=8, value=date_out)
            done.append(str(rec["Serial"]))
        if done:
            self.save()
        return done

    def create_return(self, serial: str, reason: str, condition: str,
                      notes: str, action: str, date: str):
        """Return a sold unit: action 'restock' puts it back In Stock,
        'quarantine' pulls it aside. Returns (record, error)."""
        key = serial.strip().lower()
        rec = next((r for r in self.records
                    if str(r["Serial"]).strip().lower() == key
                    and str(r["Status"]).strip() == SOLD), None)
        if rec is None:
            return None, "No sold unit found for serial " + serial
        action_label = "Restocked" if action == "restock" else "Quarantined"
        self.ret_sheet.append([str(rec["Serial"]), str(rec["Model"]),
                               str(rec["Customer"]), reason, condition,
                               notes, action_label, date])
        self.returns.append({
            "Serial": rec["Serial"], "Model": rec["Model"],
            "Customer": rec["Customer"], "Reason": reason,
            "Condition": condition, "Notes": notes,
            "Action": action_label, "Date": date})
        row = rec["_row"]
        if action == "restock":
            rec["Status"] = IN_STOCK
            rec["Customer"] = ""
            rec["Date Out"] = ""
            self.recs.cell(row=row, column=6, value=IN_STOCK)
            self.recs.cell(row=row, column=7, value="")
            self.recs.cell(row=row, column=8, value="")
        else:
            rec["Status"] = QUARANTINED
            self.recs.cell(row=row, column=6, value=QUARANTINED)
        self.save()
        return rec, None

    # ---------------------------------------------------------- brands
    def assign_brand(self, model: str, brand: str):
        """Remember Model -> Brand permanently (exact full string match)."""
        if brand not in self.brands:
            self.brands.append(brand)
            self.brands_sheet.append([brand])
        key = model.strip().upper()
        if self.model_to_brand.get(key) != brand:
            self.model_to_brand[key] = brand
            self.map_sheet.append([model.strip(), brand])
        self.save()

    def add_brand(self, brand: str) -> bool:
        brand = brand.strip()
        if not brand or brand in self.brands:
            return False
        self.brands.append(brand)
        self.brands_sheet.append([brand])
        self.save()
        return True
