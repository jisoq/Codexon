import QtQuick

Item {
    id: root
    required property var plot
    QuotaDetail {
        id: chartCard
        plot: root.plot
        companionHeight: shareCard.visible ? shareCard.height + 8 : 0
    }
    QuotaDetail {
        id: shareCard
        plot: root.plot
        shareCard: true
        primaryCard: chartCard
    }
}
