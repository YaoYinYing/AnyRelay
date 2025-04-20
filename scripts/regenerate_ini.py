#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
from dataclasses import dataclass
from typing import List


continent_flag_dict={
    "亚洲": "🗼",
    "欧洲": "🏰",
    "美洲": "🗽",
    "非洲": "🪘",
    "大洋洲": "🦘",
    "南极洲": "🐧",
    "其他": "🗿",
}


@dataclass(frozen=True)
class Node:
    """
    group   : 洲际/地区分组 (e.g. '亚洲')
    flag    : 旗帜emoji    (e.g. '🇯🇵')
    region  : 国家或地区   (e.g. '日本')
    airport : 对应机场     (e.g. '东京国际机场')
    keyword : 节点名匹配关键字，用'|'分隔
    """
    flag: str
    region: str
    airport: str
    keyword: str
    continent: str

    @classmethod
    def from_dict(cls, data: dict) -> "Node":
        """
        从字典创建 Node 对象
        """
        return cls(
            flag=data["flag"].strip(),
            region=data["region"].strip(),
            airport=data["airport"].strip(),
            keyword=data["keyword"].strip(),
            continent=data["continent"].strip(),
        )
    
    @classmethod
    def merge_as_continent(cls, nodes: List["Node"]) -> "Node":
        """
        合并多个节点为一个大节点，group 为洲际/地区分组，其他字段为空
        """
        return cls(
            flag=continent_flag_dict[nodes[0].continent],
            region=nodes[0].continent,
            airport=f'{nodes[0].continent}随心飞机场',
            keyword="|".join(node.keyword for node in nodes if node.continent == nodes[0].continent),
            continent=nodes[0].continent,
        )
    
    @property
    def lb_node_name_in_table(self) -> str:
        """
        生成节点在表格中的名称
        """
        return f"`[]{self.flag} {self.region}节点"
    
    @property
    def relay_node_name_in_table(self) -> str:
        """
        生成节点在表格中的名称
        """
        return f"`[]{self.flag} {self.airport}"

    @property
    def loadbalance_node(self) -> str:
        """
        负载均衡组: 用于给 {flag}{region}节点 生成 load-balance 规则
        示例:
        custom_proxy_group=🇯🇵 日本节点`load-balance`(日本|川日|...)
        `[]REJECT`http://www.gstatic.com/generate_204`6537,,50`round-robin
        """
        return (
            f"custom_proxy_group={self.flag} {self.region}节点`load-balance`"
            f"({self.keyword})"
            "`[]REJECT`http://www.gstatic.com/generate_204`6537,,50`round-robin"
        )

    @property
    def urltest_node(self) -> str:
        """
        测速组 (如需单独生成 url-test 规则，可参考此处)
        这里仅作示例，具体可根据需要进行修改
        """
        return (
            f"custom_proxy_group={self.flag} {self.region}节点`url-test`"
            f"({self.keyword})"
            "`[]REJECT`http://www.gstatic.com/generate_204`300,,50"
        )

    @property
    def relaynode(self) -> str:
        """
        中继节点组: 用于 {flag}{airport} 与 {flag}{region}节点 的 relay
        示例:
        custom_proxy_group=🇯🇵 东京国际机场`relay`[]🇯🇵 日本节点`[]🛬 国际到达
        """
        return (
            f"custom_proxy_group={self.flag} {self.airport}`relay`"
            f"[]{self.flag} {self.region}节点`[]🛬 国际到达"
        )

def generate_ini(nodes: List[Node], template_content: str, use_node_lb:bool=False) -> str:
    """
    根据节点信息生成最终的 INI 内容
    """

    # 1) 🍺 全部节点（测速1） -> 收集所有 keyword，并用 '|' 拼接
    #    比如日本|川日|东京... + 新加坡|坡|狮城... + ...
    all_keywords = []
    for node in nodes:
        # node.keyword 可能包含多组关键字，用 '|' 分隔
        # 此处可以直接合并，也可以选择拆分后再合并以去重
        all_keywords.append(node.keyword)
    # 将所有关键字合并成一个大字符串 (注意去重或其他处理)
    # 这里简单拼接，用 '|' 分隔
    merged_keywords = "|".join(all_keywords)
    all_continents_in_nodes=set([node.continent for node in nodes])
    continent_nodes=[
        Node.merge_as_continent([node for node in nodes if node.continent == continent])
        for continent in continent_flag_dict if continent in all_continents_in_nodes
    ]
    global_node=Node(flag='🏁',region='全球',airport='全球随心飞机场',keyword=merged_keywords,continent='全球')
    unreconized_node=Node(flag='🤡',region='未识别',airport='未识别',keyword=f'^((?!{merged_keywords}).)*$',continent='其他')

    # 🍺 全部节点（测速1）
    # 例如:
    # custom_proxy_group=🍺 全部节点（测速1）`url-test`(merged_keywords)
    # `[]REJECT`http://www.gstatic.com/generate_204`600,,50`round-robin
    speed_test_section_all_nodes = (
        "custom_proxy_group=🍺 全部节点（测速1）`url-test`"
        f"({merged_keywords})"
        "`[]REJECT`http://www.gstatic.com/generate_204`600,,50`round-robin\n"
    )

    # 2) 🍷 负载均衡（测速2） -> 每个 Node 都会生成一个 load-balance 规则
    #    同一个国家可能在 CSV 多行重复出现，则取决于数据是否去重

    speed_test_section_loadbalance_lines = (
        "custom_proxy_group=🍷 负载均衡（测速2）`url-test"
        f"{''.join(node.lb_node_name_in_table for node in [global_node]+continent_nodes+nodes+[unreconized_node])}"
        "`http://www.gstatic.com/generate_204`6100,,50"
    )


    # 3) 🥂 转发节点（测速3） -> 每个 Node 生成 relay 规则
    speed_test_section_relay_lines = (
        'custom_proxy_group=🥂 转发节点（测速3）`url-test'
        f"{''.join(node.relay_node_name_in_table for node in [global_node]+continent_nodes+nodes+[unreconized_node])}"
        '`http://www.gstatic.com/generate_204`620,,50'
    )
    
    # 4) ☑️ 手动切换 -> 包含所有 🍷 + 🥂 的节点名
    #    例如: custom_proxy_group=☑️ 手动切换`url-test`[]🇯🇵 日本节点`[]🇯🇵 东京国际机场`...
    #    此处也可以直接用 select / url-test，看你的需要
    switch_lines = (
        "custom_proxy_group=☑️ 手动切换`url-test`"
        f"{''.join(node.lb_node_name_in_table for node in [global_node]+continent_nodes+nodes+[unreconized_node])}"
        f"{''.join(node.relay_node_name_in_table for node in [global_node]+continent_nodes+nodes+[unreconized_node])}"
        "`http://www.gstatic.com/generate_204`6000,,50\n"
    )

    # 5) 🛫 国际出发 -> 包含所有 🥂 的节点名
    #    例如: custom_proxy_group=🛫 国际出发`url-test`[]🇯🇵 东京国际机场`...
    departure_lines = (
        "custom_proxy_group=🛫 国际出发`url-test`"
        f"{''.join(node.relay_node_name_in_table for node in [global_node]+continent_nodes+nodes+[unreconized_node])}"
        "`http://www.gstatic.com/generate_204`5000,,50\n"
    )

    # 6) LB_NODES -> 负载均衡组
    lb_nodes_list=[]

    lb_nodes_list.append('\n\n;全球负载均衡组')
    lb_nodes_list.append(global_node.loadbalance_node if use_node_lb else global_node.urltest_node)

    for n in continent_nodes:
        lb_nodes_list.append(f'\n\n;大洲负载均衡组：{n.continent}')
        lb_nodes_list.append(n.loadbalance_node if use_node_lb else n.urltest_node)


    for c in continent_flag_dict:
        if c not in all_continents_in_nodes:
            continue
        lb_nodes_list.append(f'\n\n;地区负载均衡组：{c}')
        lb_nodes_list.extend([node.loadbalance_node if use_node_lb else node.urltest_node for node in nodes if node.continent==c])


    lb_nodes='\n'.join(lb_nodes_list)

    # 7) RELAY_NODES -> 中继节点组
    relay_nodes_list=[]

    relay_nodes_list.append('\n\n;全球中继节点组')
    relay_nodes_list.append(global_node.relaynode)
    
    
    for n in continent_nodes:
        relay_nodes_list.append(f'\n\n;大洲中继节点组：{n.continent}')
        relay_nodes_list.append(n.relaynode)

    for c in continent_flag_dict:
        if c not in all_continents_in_nodes:
            continue
        relay_nodes_list.append(f'\n\n;地区中继节点组：{c}')
        relay_nodes_list.extend([n.relaynode for n in nodes if n.continent==c])

    relay_nodes_list.append('\n\n;未识别中继节点组')
    relay_nodes_list.append(unreconized_node.relaynode)

    relay_nodes='\n'.join(relay_nodes_list)


    # 8) NODE_LIST -> 节点列表
    node_list=''.join(node.lb_node_name_in_table for node in nodes+continent_nodes)

    # 9) ASIA_NODE -> 亚洲节点
    asian_node=''.join(node.lb_node_name_in_table for node in continent_nodes if node.region=='亚洲')
    
    # 10) GLOBAL_NODE_GROUP -> 全球节点
    global_node_group=global_node.lb_node_name_in_table

    # 11）混！合！在！一！起！！！
    replace_dict={
        "SPEEDTEST_GROUP_1": speed_test_section_all_nodes,
        "SPEEDTEST_GROUP_2": speed_test_section_loadbalance_lines,
        "SPEEDTEST_GROUP_3": speed_test_section_relay_lines,
        "MANUAL_GROUP_1": switch_lines,
        "MANUAL_GROUP_2": departure_lines,
        "LB_NODE_GROUP": lb_nodes,
        "RELAY_GROUP": relay_nodes,
        "NODE_LIST": node_list,
        "ASIAN_NODE": asian_node,
        "GLOBAL_NODE_GROUP": global_node_group,
        "UNRECOGNIZED_GROUP": unreconized_node.loadbalance_node if use_node_lb else unreconized_node.urltest_node,
    }

    for k,v in replace_dict.items():
        template_content=template_content.replace(f'###{k}###',v)

    return template_content

def get_all_nodes(csv_path: str) -> List[Node]:
    nodes: List[Node] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            node = Node(
                flag=row["flag"].strip(),
                region=row["region"].strip(),
                airport=row["airport"].strip(),
                keyword=row["keyword"].strip(),
                continent=row["continent"].strip(),
            )
            nodes.append(node)
    return nodes

def main():
    """
    主函数：读取 CSV，生成 Node 列表，然后输出 INI
    """
    csv_path='data/nodes.csv'
    template_path='data/relay_template.ini'

    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    # 读取 CSV 文件，生成 Node 列表
    nodes = get_all_nodes(csv_path)
    sorted_nodes=sorted(nodes,key=lambda x:x.continent)


    for ret,use_lb in {
        "config/relay.ini": True,
        "config/relay_no_lb.ini": False}.items():
        ini_content = generate_ini(sorted_nodes, template_content,use_node_lb=use_lb)
        with open(ret, "w", encoding="utf-8") as f:
            f.write(ini_content)

if __name__ == "__main__":

    main()
