package com.acme.p0.calls;

public class ConstructorCaller {
    public void createService() {
        new ConstructorService();
        new ConstructorService(1);
    }
}
