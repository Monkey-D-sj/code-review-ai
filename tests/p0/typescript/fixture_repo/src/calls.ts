import { fetchUser, parseUser } from "./api";

export function topLevelCall(id: string): string {
  return fetchUser(id);
}

export function branchCall(id: string, enabled: boolean): string {
  if (enabled) {
    return fetchUser(id);
  }
  return id;
}

export function loopCall(items: string[]): string[] {
  const result: string[] = [];
  for (const item of items) {
    result.push(parseUser(item));
  }
  return result;
}

export function tryCall(id: string): string {
  try {
    return fetchUser(id);
  } catch {
    return parseUser(id);
  }
}

export const arrowCall = (id: string): string => fetchUser(id);

export async function asyncCall(id: string): Promise<string> {
  return await fetchUserAsync(id);
}

export class LocalStore {
  constructor(private readonly seed: string) {
    parseUser(seed);
  }

  load(): string {
    return this.save(this.seed);
  }

  save(value: string): string {
    return value;
  }

  static create(seed: string): LocalStore {
    return new LocalStore(seed);
  }
}

export function instanceMethodCall(seed: string): string {
  const store: LocalStore = new LocalStore(seed);
  return store.load();
}

export function constructorCall(seed: string): LocalStore {
  return new LocalStore(seed);
}

export function crossFileAliasCall(id: string): string {
  return fetchUser(id);
}

export function dynamicKeyCall(value: Record<string, () => string>, key: string): string {
  return value[key]();
}

export function reflectionCall(value: object, key: string): unknown {
  return Reflect.get(value, key)();
}

export function higherOrderCall(callback: () => string): string {
  return runTask(callback);
}

function runTask(callback: () => string): string {
  return callback();
}

function fetchUserAsync(id: string): Promise<string> {
  return Promise.resolve(id);
}
