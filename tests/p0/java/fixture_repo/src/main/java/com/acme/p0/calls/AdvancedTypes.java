package com.acme.p0.calls;

enum Mode {
    FAST,
    SAFE
}

record Point(int x, int y) {}

class InitBox {
    static {
        bootstrap();
    }

    {
        bootstrap();
    }

    static void bootstrap() {}
}

enum Behavior {
    DEFAULT;

    void run() {
        helper();
    }

    void helper() {}
}

record RecordValue(int value) {
    RecordValue {}
}

class RecordCaller {
    Point make() {
        return new RecordValue(1);
    }
}
