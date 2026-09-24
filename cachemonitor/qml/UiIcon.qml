import QtQuick
Canvas {
    id: icon
    property string name: ""
    property color ink: appTheme.palette.ink
    implicitWidth: 20; implicitHeight: 20
    onNameChanged: requestPaint()
    onInkChanged: requestPaint()
    onWidthChanged: requestPaint()
    onPaint: {
        let c=getContext("2d"); c.reset();c.scale(width/24,height/24)
        c.strokeStyle=ink;c.lineWidth=1.8;c.lineCap="round";c.lineJoin="round";c.beginPath()
        if(name==="home") { c.moveTo(3,11);c.lineTo(12,3);c.lineTo(21,11);c.moveTo(6,9);c.lineTo(6,21);c.lineTo(10,21);c.lineTo(10,15);c.lineTo(14,15);c.lineTo(14,21);c.lineTo(18,21);c.lineTo(18,9) }
        else if(name==="back" || name==="left") { c.moveTo(13,5);c.lineTo(6,12);c.lineTo(13,19);if(name==="back"){c.moveTo(6,12);c.lineTo(21,12)} }
        else if(name==="right") { c.moveTo(9,5);c.lineTo(16,12);c.lineTo(9,19) }
        else if(name==="close") { c.moveTo(6,6);c.lineTo(18,18);c.moveTo(18,6);c.lineTo(6,18) }
        else if(name==="plus" || name==="minus") { c.moveTo(5,12);c.lineTo(19,12);if(name==="plus"){c.moveTo(12,5);c.lineTo(12,19)} }
        else if(name==="calendar") { c.rect(4,5,16,16);c.moveTo(4,10);c.lineTo(20,10);c.moveTo(8,3);c.lineTo(8,7);c.moveTo(16,3);c.lineTo(16,7) }
        else if(name==="external") { c.moveTo(14,4);c.lineTo(20,4);c.lineTo(20,10);c.moveTo(20,4);c.lineTo(10,14);c.moveTo(10,5);c.lineTo(4,5);c.lineTo(4,20);c.lineTo(19,20);c.lineTo(19,14) }
        else if(name==="restart") { c.arc(12,12,8,-Math.PI/3,Math.PI*1.5);c.moveTo(12,4);c.lineTo(16,4);c.lineTo(16,8) }
        c.stroke()
    }
}
