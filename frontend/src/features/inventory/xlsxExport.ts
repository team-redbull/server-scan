import { strToU8, zipSync } from "fflate";

import type { ServerRow } from "@/types/server";

/** Column order, header text, and value accessor for the inventory export. */
const COLUMNS: { header: string; value: (row: ServerRow) => string }[] = [
  { header: "Name", value: (r) => r.name },
  { header: "Vendor", value: (r) => r.vendor },
  { header: "BMC address", value: (r) => r.bmc_host ?? "" },
  { header: "Model", value: (r) => r.model ?? "" },
  { header: "Serial", value: (r) => r.serial ?? "" },
  { header: "SPT", value: (r) => r.profile_template_name ?? "" },
  { header: "Installation", value: (r) => r.openshift_state },
  { header: "MCE", value: (r) => r.mce_name ?? "" },
  { header: "Cluster", value: (r) => r.cluster_name ?? "" },
  { header: "State", value: (r) => r.health },
];

const SHEET_NAME = "Servers";
const MIN_WIDTH = 8;
const MAX_WIDTH = 50;
const XLSX_MIME =
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/** Column letter(s) for a zero-based index: 0 -> A, 25 -> Z, 26 -> AA. */
function columnLetter(index: number): string {
  let n = index;
  let letters = "";
  do {
    letters = String.fromCharCode(65 + (n % 26)) + letters;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return letters;
}

/** Whether XML 1.0 allows `code` (tab, newline and carriage return are the only control characters it does). */
function isXmlChar(code: number): boolean {
  return code >= 0x20 || code === 0x09 || code === 0x0a || code === 0x0d;
}

/** Escapes XML text and drops the control characters XML 1.0 forbids. */
function xmlText(value: string): string {
  let clean = "";
  for (let i = 0; i < value.length; i++) {
    if (isXmlChar(value.charCodeAt(i))) clean += value[i];
  }
  return clean
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** One text cell. Inline strings keep every value text, so Excel never turns
 * an IP, a serial with leading zeros or a leading `=` into a number or formula. */
function textCell(ref: string, value: string, style: number): string {
  if (value === "") return "";
  return `<c r="${ref}" t="inlineStr" s="${style}"><is><t xml:space="preserve">${xmlText(value)}</t></is></c>`;
}

/** Width in Excel's character units: the longest value in the column, header
 * included, plus a little padding, kept between `MIN_WIDTH` and `MAX_WIDTH`. */
function columnWidth(header: string, values: string[]): number {
  let longest = header.length + 1;
  for (const value of values) {
    longest = Math.max(longest, value.length);
  }
  return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, longest + 2));
}

/** Builds the `.xlsx` bytes for `rows`, in the order given (the caller decides
 * which filter/sort state that reflects): bold frozen header, filter buttons,
 * and each column as wide as its longest value, up to a cap. */
export function rowsToXlsx(rows: ServerRow[]): Uint8Array {
  const table = rows.map((row) => COLUMNS.map((c) => c.value(row)));
  const widths = COLUMNS.map((c, i) =>
    columnWidth(
      c.header,
      table.map((cells) => cells[i] ?? ""),
    ),
  );
  const lastColumn = columnLetter(COLUMNS.length - 1);
  const lastRow = rows.length + 1;

  const header = COLUMNS.map((c, i) =>
    textCell(`${columnLetter(i)}1`, c.header, 1),
  ).join("");
  const body = table
    .map(
      (cells, r) =>
        `<row r="${r + 2}">${cells
          .map((value, i) => textCell(`${columnLetter(i)}${r + 2}`, value, 0))
          .join("")}</row>`,
    )
    .join("");
  const cols = widths
    .map(
      (w, i) =>
        `<col min="${i + 1}" max="${i + 1}" width="${w}" customWidth="1"/>`,
    )
    .join("");

  const sheet =
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
    `<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>` +
    `<cols>${cols}</cols>` +
    `<sheetData><row r="1">${header}</row>${body}</sheetData>` +
    `<autoFilter ref="A1:${lastColumn}${lastRow}"/>` +
    `</worksheet>`;

  const workbook =
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">` +
    `<sheets><sheet name="${SHEET_NAME}" sheetId="1" r:id="rId1"/></sheets>` +
    `<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">${SHEET_NAME}!$A$1:$${lastColumn}$${lastRow}</definedName></definedNames>` +
    `</workbook>`;

  const styles =
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
    `<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>` +
    `<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>` +
    `<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>` +
    `<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>` +
    `<cellXfs count="2"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="49" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyNumberFormat="1"/></cellXfs>` +
    `<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>` +
    `</styleSheet>`;

  const contentTypes =
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
    `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>` +
    `<Default Extension="xml" ContentType="application/xml"/>` +
    `<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>` +
    `<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>` +
    `<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>` +
    `</Types>`;

  const rootRels =
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
    `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>` +
    `</Relationships>`;

  const workbookRels =
    `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
    `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>` +
    `<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>` +
    `</Relationships>`;

  return zipSync({
    "[Content_Types].xml": strToU8(contentTypes),
    "_rels/.rels": strToU8(rootRels),
    "xl/workbook.xml": strToU8(workbook),
    "xl/_rels/workbook.xml.rels": strToU8(workbookRels),
    "xl/styles.xml": strToU8(styles),
    "xl/worksheets/sheet1.xml": strToU8(sheet),
  });
}

/** Triggers a browser download of `content` as a file named `filename`. */
export function downloadXlsx(filename: string, content: Uint8Array): void {
  const blob = new Blob([content as BlobPart], { type: XLSX_MIME });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
