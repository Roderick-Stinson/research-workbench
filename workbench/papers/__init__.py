"""论文侧（ADR-0014 起）：收录、精选、精读、领域地图、构思、复现。

数据都在 $WB_HOME 下（papers/、reports/、map/、problems/、ideas/、registry/field.json、registry/seeds.json）。
核心只用标准库；外部请求走 http.get（带本地缓存与按站点限速）。
"""
