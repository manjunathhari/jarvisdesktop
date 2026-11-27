// Where each panel goes in the Board's panel area. Pure: no DOM.
//
// A maximised panel takes the whole area and the others get nothing. Otherwise the open
// panels are tiled in at most two columns, in the order given (the Board passes the newest
// first); a last panel alone on its row spans it. Minimised and closed panels get no
// rectangle. A row is never lower than MIN_ROW: with many panels the area scrolls.

export const MIN_ROW = 260;

export interface Tiled {
  id: string;
  state: string;
}

export interface Rect {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export function tile(panels: readonly Tiled[], width: number, height: number): Rect[] {
  const maximised = panels.find((p) => p.state === "maximised");
  if (maximised) {
    return [{ id: maximised.id, x: 0, y: 0, width, height: Math.max(height, MIN_ROW) }];
  }
  const open = panels.filter((p) => p.state === "open");
  if (open.length === 0) return [];
  const columns = Math.min(2, open.length);
  const rows = Math.ceil(open.length / columns);
  const rowHeight = Math.max(Math.floor(height / rows), MIN_ROW);
  const columnWidth = Math.floor(width / columns);
  return open.map((p, i) => {
    const row = Math.floor(i / columns);
    const column = i % columns;
    const alone = i === open.length - 1 && column === 0;
    return {
      id: p.id,
      x: column * columnWidth,
      y: row * rowHeight,
      width: alone ? width : columnWidth,
      height: rowHeight,
    };
  });
}
