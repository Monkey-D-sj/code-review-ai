package com.example.domain;

public enum PetType {

    CAT {
        @Override
        public String sound() {
            return "meow";
        }
    },
    DOG {
        @Override
        public String sound() {
            return "woof";
        }
    },
    LIZARD;

    public String sound() {
        return "...";
    }
}
