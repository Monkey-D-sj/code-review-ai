package com.example;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest
public class OwnerControllerTests {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void listOwnersOk() throws Exception {
        mockMvc.perform(get("/owners?page=1")).andExpect(status().isOk());
    }

    @Test
    void showOwnerOk() throws Exception {
        mockMvc.perform(get("/owners/7")).andExpect(status().isOk());
    }
}
