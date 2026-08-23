package com.acme.p0.calls;

class ChainTarget {
    void run() {
    }
}

class ChainFactory {
    ChainTarget create() {
        return null;
    }
}

public class ReturnChain {
    void invoke(ChainFactory factory) {
        factory.create().run();
    }
}
