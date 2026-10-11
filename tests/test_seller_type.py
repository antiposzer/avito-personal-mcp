import pytest

from avito_personal_mcp.seller_type import infer_seller_type


@pytest.mark.parametrize(
    "description",
    [None, "", "Только телефон, коробки нет.", "В отличном состоянии. Аккумулятор 90%."],
)
def test_short_or_missing_description_is_not_proof_of_private_seller(description):
    assert infer_seller_type(description)["type"] == "unknown"


def test_own_phone_and_purchase_from_shop_are_private_clues():
    result = infer_seller_type("Продаю свой телефон. Купил в фирменном магазине. Гарантия Apple.")
    assert result["type"] == "private"
    assert result["confidence"] == "medium"


def test_negative_shop_claim_is_not_a_store():
    result = infer_seller_type("Я не магазин. Продаю личный телефон, пользовался сам.")
    assert result["type"] == "private"


def test_store_representing_itself_takes_precedence_over_personal_copy():
    result = infer_seller_type(
        "Пользовался аккуратно. Приходите в наш магазин, принимаем оплату картой."
    )
    assert result["type"] == "shop"
    assert result["confidence"] == "high"


def test_lookalikes_are_normalized_but_evidence_preserves_original_text():
    description = "Bac приветствует фирменный мaгaзин техники. Рассрочка."
    result = infer_seller_type(description)
    assert result["type"] == "shop"
    assert "мaгaзин" in result["evidence"][0]["excerpt"]


def test_buyer_with_warranty_is_not_a_store():
    result = infer_seller_type("Продаю свой телефон, гарантия производителя ещё действует.")
    assert result["type"] == "private"


def test_platform_financing_alone_does_not_prove_shop():
    assert infer_seller_type("Доступна рассрочка через Авито.")["type"] == "unknown"


def test_multiple_distinct_commercial_clues_are_required():
    assert infer_seller_type("Даем гарантию. Даем гарантию. Даем гарантию.")["type"] == "unknown"
    result = infer_seller_type("Даем гарантию 14 дней. Работаем каждый день с 10 до 20.")
    assert result["type"] == "shop"
    assert result["confidence"] == "medium"


def test_retail_network_and_buyback_are_strong_clues():
    assert infer_seller_type("Мы — сеть салонов электроники.")["type"] == "shop"
    assert infer_seller_type("Выгодно оценим в трейд-ин ваш айфон.")["type"] == "shop"


def test_explicit_import_business_is_not_mistaken_for_private_gift():
    result = infer_seller_type("Прямые поставки из заграницы. Подарок при покупке. В наличии.")
    assert result["type"] == "shop"


def test_professional_seller_is_commercial_even_without_physical_store():
    result = infer_seller_type(
        "Я занимаюсь яблочной продукцией с 2018 года. Рассрочка через банки."
    )
    assert result["type"] == "shop"
