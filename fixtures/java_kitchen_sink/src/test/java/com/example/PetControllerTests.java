package com.example;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.web.servlet.MockMvc;

public class PetControllerTests {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void initCreationFormOk() throws Exception {
        mockMvc.perform(get("/owners/7/pets/new")).andExpect(status().isOk());
    }

    @Test
    void processCreationFormOk() throws Exception {
        mockMvc.perform(post("/owners/7/pets/new")).andExpect(status().isOk());
    }

    @ParameterizedTest
    @ValueSource(strings = {"first", "second"})
    void petTypeName(String name) {
        if (name.isEmpty()) {
            throw new IllegalStateException(name);
        }
    }
}
