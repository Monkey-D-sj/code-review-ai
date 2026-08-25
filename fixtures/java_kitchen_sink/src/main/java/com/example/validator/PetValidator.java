package com.example.validator;

import com.example.domain.Pet;
import org.springframework.validation.Errors;
import org.springframework.validation.Validator;

public class PetValidator implements Validator {

    @Override
    public boolean supports(Class<?> clazz) {
        return Pet.class.isAssignableFrom(clazz);
    }

    @Override
    public void validate(Object target, Errors errors) {
        Pet pet = (Pet) target;
        if (pet.getName() == null || pet.getName().isBlank()) {
            errors.rejectValue("name", "required");
        }
    }
}
