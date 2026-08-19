package com.acme.p0.calls;

public class Recursion {
    public void direct() {
        direct();
    }

    public void even() {
        odd();
    }

    public void odd() {
        even();
    }
}
