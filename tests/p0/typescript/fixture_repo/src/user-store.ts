import { BaseStore } from "./base-store";
import type { PersistableStore, ReadableStore } from "./contracts";

export class UserStore extends BaseStore implements ReadableStore, PersistableStore {
  read(): string {
    return super.read();
  }

  persist(value: string): string {
    return value;
  }
}
