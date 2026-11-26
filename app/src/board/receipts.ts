// When the Board owes the desktop a "drawn" report. Pure: no DOM, no calls to the app.
//
// A report is owed only for a panel that arrived by an event, and is given only when that
// revision is really on the page: once, never on the strength of an older drawing, and never
// for a drawing that has left the page.

export class Receipts {
  /// Arrived by an event, report still owed: panel -> revision.
  private owed = new Map<string, number>();
  /// On the page with its body drawn: panel -> revision.
  private onPage = new Map<string, number>();

  /// A show or update event came. True: that revision is already drawn, report it now.
  arrived(id: string, revision: number): boolean {
    if (this.onPage.get(id) === revision) return true;
    this.owed.set(id, revision);
    return false;
  }

  /// A body finished drawing (`ok`) or could not be drawn. True: a report is owed for it.
  filled(id: string, revision: number, ok: boolean): boolean {
    if (ok) this.onPage.set(id, revision);
    if (this.owed.get(id) !== revision) return false;
    this.owed.delete(id);
    return true;
  }

  /// The panel's drawing left the page (minimised, closed, covered, replaced).
  removed(id: string): void {
    this.onPage.delete(id);
  }
}
