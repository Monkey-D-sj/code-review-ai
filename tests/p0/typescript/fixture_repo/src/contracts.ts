export interface ReadableStore {
  read(): string;
}

export interface PersistableStore {
  persist(value: string): string;
}
