package com.example.domain;

import com.example.core.BaseEntity;

public class Pet extends BaseEntity {

    private String name;
    private PetType type;

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public PetType getType() {
        return type;
    }

    public void setType(PetType type) {
        this.type = type;
    }
}
