export class BaseStore {
  protected baseValue = "base";

  read(): string {
    return this.baseValue;
  }
}

export interface AuditableStore {
  audit(): string;
}
