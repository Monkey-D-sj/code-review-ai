package com.example.domain;

import com.example.core.BaseEntity;

public class Owner extends BaseEntity {

    private String lastName;

    public String getLastName() {
        return lastName;
    }

    public void setLastName(String lastName) {
        this.lastName = lastName;
    }
}
