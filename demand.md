# 项目重点

## 解析
parse逐个解析文件，并返回原始内容：确定的source(qname)


## 查询
无法覆盖的情况怎么返回
确定的怎么返回



## 这个项目应该实现什么目标

普通场景，比如说只看当前，或者一跳上下文，可以很简单的通过read/grep/rg等工具解决

native解决慢的场景

## 多跳场景
- 比如说改动了schema，controller-service-curd-schema这条链路，反向遍历，给出那些接口需要测试
当然不止schema，utils啥的也算 
- 接口传入字段修改，controller-service-curd-schema/table，最后的table/schema字段不一样，报错（通过skill）

## 循环引入
A->B，然后B里面又引入A
实现：a.py引入b（21行import，b在21行引入），b局部引入a

## 死代码
删除是否对其他地方没影响，当然如果没有重名的情况，rg可以做到，有重名就得一个个看了


## 扇出过多
巨型节点，建议拆分



