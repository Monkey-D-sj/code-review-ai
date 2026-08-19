package com.acme.p0.negative;

import java.lang.reflect.Method;
import java.lang.reflect.Proxy;

public class DynamicCalls {
    public void normalTarget() {}
    public void applyCallback(Runnable task) {}

    public void negativeConsumer(Runnable task) throws Exception {
        normalTarget();
        applyCallback(task);
        Class.forName("com.acme.p0.negative.DynamicCalls");
        Proxy.newProxyInstance(null, null, null);
        Method method = getMethod("normalTarget");
        method.invoke(this);
    }

    private Method getMethod(String name) {
        return null;
    }
}
