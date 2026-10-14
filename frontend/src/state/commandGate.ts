/** Synchronous lock for commands that React has not yet had time to re-render as disabled. */
export class CommandGate {
  private active = false;
  get locked() { return this.active; }
  acquire(): boolean {
    if (this.active) return false;
    this.active = true;
    return true;
  }
  release() { this.active = false; }
}
