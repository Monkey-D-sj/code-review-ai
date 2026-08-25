package com.example.controller;

import com.example.domain.Pet;
import com.example.validator.PetValidator;
import jakarta.validation.Valid;
import org.springframework.stereotype.Controller;
import org.springframework.validation.BindingResult;
import org.springframework.web.bind.WebDataBinder;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.InitBinder;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;

@Controller
@RequestMapping("/owners/{ownerId}")
public class PetController {

    private final PetValidator petValidator;

    public PetController(PetValidator petValidator) {
        this.petValidator = petValidator;
    }

    @InitBinder
    public void setAllowedFields(WebDataBinder dataBinder) {
        dataBinder.setDisallowedFields("id");
    }

    @GetMapping("/pets/new")
    public String initCreationForm() {
        return "pets/createOrUpdatePetForm";
    }

    @PostMapping("/pets/new")
    public String processCreationForm(@Valid Pet pet, BindingResult result) {
        if (result.hasErrors()) {
            return "error";
        }
        petValidator.validate(pet, result);
        return "redirect:/owners/7";
    }
}
