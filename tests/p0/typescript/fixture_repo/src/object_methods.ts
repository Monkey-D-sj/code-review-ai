function objectTarget(): string {
  return "target";
}

const objectHolder = {
  run(): string {
    return objectTarget();
  },
  fn: () => objectTarget(),
};

export function objectMethodCall(): string {
  return objectHolder.run();
}

export function objectFunctionCall(): string {
  return objectHolder.fn();
}

function iifeTarget(): string {
  return "iife";
}

export function iifeCall(): string {
  return (() => iifeTarget())();
}
