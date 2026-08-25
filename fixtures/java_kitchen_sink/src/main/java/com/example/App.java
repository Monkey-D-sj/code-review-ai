package com.example;

import com.example.controller.OwnerController;
import com.example.repo.OwnerRepositoryImpl;
import com.example.service.ClinicServiceImpl;
import com.example.util.CommonUtil;

public class App {

    public static void main(String[] args) {
        OwnerController controller =
                new OwnerController(new ClinicServiceImpl(new OwnerRepositoryImpl()));
        String owners = controller.findOwners();
        String trimmed = CommonUtil.trim(owners);
        System.out.println(CommonUtil.upper(trimmed));
    }
}
