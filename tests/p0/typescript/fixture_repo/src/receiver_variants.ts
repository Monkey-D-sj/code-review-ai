import { ReadableStore } from "./contracts";

export function interfaceReceiver(store: ReadableStore): string {
  return store.read();
}

export class GenericStore<T> {
  run(): string {
    return "generic";
  }
}

export function genericReceiver(store: GenericStore<string>): string {
  return store.run();
}

export function unionReceiver(
  store: ReadableStore | GenericStore<string>,
): string {
  return store.read();
}

export function structuralReceiver(store: { read(): string }): string {
  return store.read();
}

export class PrivateWorker {
  #run(): string {
    return "private";
  }

  call(): string {
    return this.#run();
  }
}
