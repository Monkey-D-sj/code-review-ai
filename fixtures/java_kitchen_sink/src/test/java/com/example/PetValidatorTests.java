package com.example;

import com.example.domain.Pet;
import com.example.validator.PetValidator;
import org.junit.jupiter.api.Test;
import org.springframework.validation.BindException;

public class PetValidatorTests {

    @Test
    void blankNameRejected() {
        Pet pet = new Pet();
        pet.setName(" ");
        PetValidator validator = new PetValidator();
        BindException errors = new BindException(pet, "pet");
        validator.validate(pet, errors);
    }
}
