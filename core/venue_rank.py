"""论文发表 venue 分级：CCF-A 会议/期刊、中科院一区期刊。

基于内置名单匹配 OpenAlex 返回的 source 名称；名单只用于排序和标注，
不等同于官方评级，前端展示时会提示「以官方名单为准」。
"""
import re

# 名称统一小写匹配，保留常见缩写与全称
CCF_A = {
    # 会议
    "cvpr", "iccv", "eccv", "neurips", "nips", "icml", "iclr", "acl", "emnlp",
    "naacl", "coling", "sigir", "www", "kdd", "icde", "vldb", "sigmod", "pods",
    "sosp", "osdi", "pldi", "popl", "oopsla", "chi", "uist", "fast", "usenix atc",
    "micro", "isca", "hpca", "asplos", "dac", "iccad", "rtss", "ccs",
    "usenix security", "ieee s&p", "ndss", "crypto", "eurocrypt", "focs", "stoc",
    "soda", "icalp", "lics", "cav", "icse", "fse", "ase", "ijcai", "aaai",
    "ecai", "icdm", "sdm", "wsdm", "icmr", "acm mm", "siggraph", "siggraph asia",
    "ubicomp", "icsoc", "icws", "vldb conference", "international conference on machine learning",
    # 期刊
    "ieee tpami", "tpami", "ijcv", "jmlr", "tmlr", "jair", "acm toplas", "jacm",
    "ieee toc", "ieee tocs", "ieee tse", "ieee tkde", "ieee tpds", "ieee tmc",
    "ieee twc", "ieee jsac", "ieee tsp", "ieee tip", "ieee tnnls", "ieee tc",
    "acm tods", "vldb journal", "ieee transactions on pattern analysis and machine intelligence",
    "international journal of computer vision", "journal of machine learning research",
    "artificial intelligence", "computational linguistics", "ieee transactions on software engineering",
}

# 中科院一区常见期刊（近似名单，每年更新，仅用于优先排序）
CAS_Q1 = {
    "ieee tpami", "tpami", "ijcv", "jmlr", "tmlr", "nature machine intelligence",
    "ieee tnnls", "ieee tip", "pattern recognition", "information fusion",
    "ieee tkde", "ieee tgrs", "ieee tits", "ieee tmm", "ieee tcsvt",
    "ieee tcvg", "acm computing surveys", "ieee communications surveys and tutorials",
    "remote sensing of environment", "isprs journal of photogrammetry and remote sensing",
    "knowledge-based systems", "expert systems with applications", "applied soft computing",
    "ieee journal of selected topics in applied earth observations and remote sensing",
    "ieee transactions on pattern analysis and machine intelligence",
    "international journal of computer vision", "journal of machine learning research",
    " ieee transactions on neural networks and learning systems",
    "ieee transactions on image processing", "ieee transactions on geoscience and remote sensing",
    "ieee transactions on intelligent transportation systems",
}

_RANK_ORDER = {"CCF-A / 中科院一区": 0, "CCF-A": 1, "中科院一区": 2, "": 3}


def _norm(name):
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def rank_venue(name):
    """返回 "CCF-A" / "中科院一区" / "CCF-A / 中科院一区" / ""。"""
    n = _norm(name)
    if not n:
        return ""
    in_ccf = n in CCF_A or any(k in n for k in (
        "tpami", "ijcv", "jmlr", "tmlr", "conference on computer vision",
        "computer vision and pattern recognition", "international conference on machine learning",
        "neural information processing systems", "learning representations",
        "association for computational linguistics", "empirical methods in natural language",
        "knowledge discovery and data mining", "world wide web conference",
        "international conference on computer aided verification",
        "ieee transactions on pattern analysis", "acm sigir", "usenix security"))
    in_q1 = n in CAS_Q1 or any(k in n for k in (
        "pattern recognition", "information fusion", "knowledge-based systems",
        "nature machine intelligence", "tnnls", "tgrs", "tits",
        "ieee transactions on image processing", "neural networks and learning systems",
        "geoscience and remote sensing", "intelligent transportation systems",
        "computing surveys", "communications surveys"))
    if in_ccf and in_q1:
        return "CCF-A / 中科院一区"
    if in_ccf:
        return "CCF-A"
    if in_q1:
        return "中科院一区"
    return ""


def rank_order(tier):
    return _RANK_ORDER.get(tier, 3)
