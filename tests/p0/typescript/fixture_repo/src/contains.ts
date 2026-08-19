export function moduleEntry(): string {
  return "entry";
}

export class Container {
  constructor() {}

  read(): string {
    return "read";
  }

  static create(): Container {
    return new Container();
  }

  static {
    const registered = true;
    if (!registered) {
      throw new Error("unreachable");
    }
  }
}
